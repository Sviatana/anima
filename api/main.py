from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import asyncpg
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

# -------------------- init --------------------
load_dotenv()

APP_TITLE = os.getenv("APP_TITLE", "ANIMA 2.0")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
DB_URL = os.getenv("DATABASE_URL", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

app = FastAPI(title=APP_TITLE)

logger = logging.getLogger("anima")
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


@app.on_event("startup")
async def _startup() -> None:
    if not DB_URL:
        logger.warning("DATABASE_URL is not set. DB features will fail.")
        return
    try:
        app.state.db_pool = await asyncpg.create_pool(
            dsn=DB_URL,
            min_size=int(os.getenv("DB_POOL_MIN", "1")),
            max_size=int(os.getenv("DB_POOL_MAX", "5")),
            command_timeout=float(os.getenv("DB_COMMAND_TIMEOUT", "15")),
        )
        logger.info("DB pool created.")
    except Exception:
        logger.exception("Failed to create DB pool.")
        raise


@app.on_event("shutdown")
async def _shutdown() -> None:
    pool = getattr(app.state, "db_pool", None)
    if pool:
        await pool.close()
        logger.info("DB pool closed.")


# -------------------- DB helpers (asyncpg) --------------------
async def _fetchval(sql: str, *params: Any) -> Any:
    pool = getattr(app.state, "db_pool", None)
    if not pool:
        raise RuntimeError("DB pool is not initialized")
    async with pool.acquire() as conn:
        return await conn.fetchval(sql, *params)


async def _fetch(sql: str, *params: Any) -> List[Dict[str, Any]]:
    pool = getattr(app.state, "db_pool", None)
    if not pool:
        raise RuntimeError("DB pool is not initialized")
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
        return [dict(r) for r in rows]


async def _execute(sql: str, *params: Any) -> str:
    pool = getattr(app.state, "db_pool", None)
    if not pool:
        raise RuntimeError("DB pool is not initialized")
    async with pool.acquire() as conn:
        return await conn.execute(sql, *params)


async def mark_update_processed(update_id: int) -> bool:
    # Returns True only if inserted first time
    status = await _execute(
        "INSERT INTO processed_updates(update_id) VALUES($1) ON CONFLICT DO NOTHING",
        update_id,
    )
    # status like: "INSERT 0 1" or "INSERT 0 0"
    return status.endswith(" 1")


# -------------------- Telegram --------------------
class TelegramUpdate(BaseModel):
    update_id: Optional[int] = None
    message: Optional[Dict[str, Any]] = None


async def tg_send(chat_id: int, text: str) -> None:
    if not TELEGRAM_TOKEN:
        logger.info("[DRY RUN] -> %s: %s", chat_id, (text or "")[:300])
        return
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": text},
            )
            r.raise_for_status()
    except Exception:
        logger.exception("Telegram send failed (chat_id=%s)", chat_id)


# -------------------- Safety & evaluator --------------------
STOP = re.compile(
    r"(политик|религ|насили|медицинск|вакцин|диагноз|лекарств|суицид)",
    re.IGNORECASE,
)
CRISIS = re.compile(
    r"(не хочу жить|самоповрежд|отчаяни|суицид|покончи|боль невыносима)",
    re.IGNORECASE,
)


def crisis_detect(t: str) -> bool:
    return bool(CRISIS.search(t or ""))


def detect_emotion(t: str) -> str:
    tl = (t or "").lower()
    if re.search(r"устал|напряж|тревож|страш|злюсь|злость|раздраж|грустн|плохо|паник", tl):
        return "tense"
    if re.search(r"спокойн|рад|легко|хорошо|класс|радост", tl):
        return "calm"
    if re.search(r"не знаю|путаюсь|сомнева|непонятно|не понимаю|затрудня", tl):
        return "uncertain"
    return "neutral"


def quality_score(user_text: str, reply: str) -> float:
    s = 0.0
    L = len(reply or "")
    if 80 <= L <= 900:
        s += 0.25
    if "?" in (reply or ""):
        s += 0.2
    if re.search(r"(слышу|вижу|понимаю|рядом|важно|чувствую)", (reply or "").lower()):
        s += 0.25
    tokens = [
        w
        for w in re.findall(r"[а-яa-z]{4,}", (user_text or "").lower())
        if w not in {"сейчас", "просто", "очень", "хочу"}
    ]
    if any(t in (reply or "").lower() for t in tokens[:6]):
        s += 0.3
    return s


# -------------------- Onboarding (KNO) --------------------
KNO: List[Tuple[str, str]] = [
    ("ei_q1", "Когда ты устаёшь — что помогает быстрее восстановиться: пообщаться с людьми 🪴 или побыть наедине ☁️?"),
    ("sn_q1", "Что тебе ближе: действовать по конкретным шагам и фактам 🎯 или ориентироваться на идею и смысл ✨?"),
    ("tf_q1", "Как ты чаще принимаешь решения: через логику и аргументы 🧠 или через чувства и внутренние ценности 💛?"),
    ("jp_q1", "Когда тебе спокойнее: когда всё чётко спланировано 📋 или когда есть свобода и импровизация 🎯?"),
    ("jp_q2", "Когда много задач: составить список заранее или пробовать и смотреть по ситуации?"),
    ("ei_q2", "Когда нужно разобраться: поговорить с кем-то или записать мысли для себя?"),
]
KNO_MAP: Dict[str, Tuple[str, str]] = {
    "ei_q1": ("E", "I"),
    "sn_q1": ("S", "N"),
    "tf_q1": ("T", "F"),
    "jp_q1": ("J", "P"),
    "jp_q2": ("J", "P"),
    "ei_q2": ("E", "I"),
}


async def ensure_user(uid: int, username: Optional[str] = None, first_name: Optional[str] = None, last_name: Optional[str] = None) -> None:
    await _execute(
        """
        INSERT INTO user_profile(user_id,username,first_name,last_name)
        VALUES($1,$2,$3,$4)
        ON CONFLICT (user_id) DO NOTHING
        """,
        uid,
        username,
        first_name,
        last_name,
    )


async def get_facts(uid: int) -> Dict[str, Any]:
    rows = await _fetch("SELECT facts FROM user_profile WHERE user_id=$1", uid)
    if not rows:
        return {}
    facts = rows[0].get("facts")
    if facts is None:
        return {}
    if isinstance(facts, dict):
        return facts
    if isinstance(facts, str):
        try:
            return json.loads(facts) or {}
        except Exception:
            return {}
    return {}


async def set_facts(uid: int, patch: Dict[str, Any]) -> None:
    facts = await get_facts(uid)
    facts.update(patch)
    await _execute("UPDATE user_profile SET facts=$1, updated_at=NOW() WHERE user_id=$2", facts, uid)


async def app_state(uid: int) -> Dict[str, Any]:
    return (await get_facts(uid)).get("app_state", {}) or {}


async def set_state(uid: int, patch: Dict[str, Any]) -> None:
    facts = await get_facts(uid)
    st = facts.get("app_state", {}) or {}
    st.update(patch)
    facts["app_state"] = st
    await _execute("UPDATE user_profile SET facts=$1, updated_at=NOW() WHERE user_id=$2", facts, uid)


async def kno_start(uid: int) -> None:
    await set_state(uid, {"kno_idx": 0, "kno_answers": {}, "kno_done": False})


async def kno_next(uid: int) -> Optional[str]:
    st = await app_state(uid)
    idx = st.get("kno_idx", 0)
    if idx is None:
        return None
    if idx >= len(KNO):
        return None
    return KNO[idx][1] + "\n\nОтветь 1 или 2, можно словами."


async def kno_register(uid: int, text: str) -> Optional[str]:
    st = await app_state(uid)
    idx = st.get("kno_idx", 0)
    if idx is None or idx >= len(KNO):
        return None

    key, _ = KNO[idx]
    t = (text or "").strip().lower()

    def pick(question_key: str, tt: str) -> int:
        if tt in {"1", "первый", "первое", "первая", "слева"}:
            return 1
        if tt in {"2", "второй", "второе", "вторая", "справа"}:
            return 2
        if question_key.startswith("ei_"):
            if re.search(r"наедин|тишин|один", tt):
                return 2
            if re.search(r"люд|общат|встреч", tt):
                return 1
        if question_key.startswith("sn_"):
            if re.search(r"факт|конкрет|шаг", tt):
                return 1
            if re.search(r"смысл|иде|образ", tt):
                return 2
        if question_key.startswith("tf_"):
            if re.search(r"логик|рацион|аргумент", tt):
                return 1
            if re.search(r"чувств|эмоци|ценност", tt):
                return 2
        if question_key.startswith("jp_"):
            if re.search(r"план|распис|контрол", tt):
                return 1
            if re.search(r"свobod|свобод|импров|спонтан", tt):
                return 2
        return 1

    answers = st.get("kno_answers", {}) or {}
    answers[key] = pick(key, t)

    idx += 1
    if idx >= len(KNO):
        axes = {"E": 0, "I": 0, "S": 0, "N": 0, "T": 0, "F": 0, "J": 0, "P": 0}
        for k, v in answers.items():
            a, b = KNO_MAP[k]
            axes[a if v == 1 else b] += 1

        def norm(a: int, b: int) -> Tuple[float, float]:
            s = a + b
            return (a / (s or 1), b / (s or 1))

        E, I = norm(axes["E"], axes["I"])
        S, N = norm(axes["S"], axes["N"])
        T, F = norm(axes["T"], axes["F"])
        J, P = norm(axes["J"], axes["P"])

        await _execute(
            """
            INSERT INTO psycho_profile(user_id,ei,sn,tf,jp,confidence,mbti_type,anchors,state)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)
            ON CONFLICT (user_id) DO UPDATE
            SET ei=EXCLUDED.ei,
                sn=EXCLUDED.sn,
                tf=EXCLUDED.tf,
                jp=EXCLUDED.jp,
                confidence=EXCLUDED.confidence,
                updated_at=NOW()
            """,
            uid,
            E,
            N,
            T,
            J,
            0.4,
            None,
            [],
            None,
        )

        await set_state(uid, {"kno_done": True, "kno_idx": None, "kno_answers": answers})
        return (
            "Спасибо, я лучше понимаю, как с тобой говорить 💛\n"
            "Уверенность 40%\n"
            "Пока это черновой профиль. Он будет уточняться по ходу диалога."
        )

    await set_state(uid, {"kno_idx": idx, "kno_answers": answers})
    return KNO[idx][1] + "\n\nОтветь 1 или 2, можно словами."


# -------------------- Dialogue engine --------------------
def comms_style(p: Dict[str, Any]) -> Dict[str, str]:
    return {
        "tone": "активный" if p.get("ei", 0.5) >= 0.5 else "спокойный",
        "detail": "смыслы" if p.get("sn", 0.5) >= 0.5 else "шаги",
        "mind": "анализ" if p.get("tf", 0.5) >= 0.5 else "чувства",
        "plan": "план" if p.get("jp", 0.5) >= 0.5 else "эксперимент",
    }


def reflect_emotion(text: str) -> str:
    t = (text or "").lower()
    if re.search(r"устал|напряж|тревож|злюсь|грустн|плохо|паник", t):
        return "Слышу напряжение и заботу о результате. "
    if re.search(r"спокойн|рад|легко|класс|хорошо", t):
        return "Чувствую спокойствие и лёгкость. "
    if re.search(r"не знаю|путаюсь|сомнева|непонятно", t):
        return "Вижу, что хочется ясности. "
    return "Я рядом и слышу тебя. "


def playful_oneline() -> str:
    jokes = [
        "Иногда лучший выбор — выбрать один микрошаг. Потому что диван уже выбрал тебя 😄",
        "Если сомневаешься — выбери вариант, где ты добрее к себе. Это почти всегда выигрыш 😉",
        "Секрет продуктивности — начать. Остальное догонит 🚶‍♀️",
        "Мозг любит завершать начатое. Запусти 10 минут — и он уже за тебя 🤖",
    ]
    return jokes[int(time.time()) % len(jokes)]


IntentFn = Callable[[Dict[str, str], bool], str]

DECISION_RX = re.compile(r"(правильн|лучший).*выбор|как.*решен|принять.*решен", re.IGNORECASE)
STRESS_RX = re.compile(r"стресс|тревог|паник|пережив|напряжен", re.IGNORECASE)
PROCRAS_RX = re.compile(r"прокраст|не могу начать|откладыва", re.IGNORECASE)
GOALS_RX = re.compile(r"цель|план|стратеги|куда двигаться|приоритет", re.IGNORECASE)
BOUNDARY_RX = re.compile(r"границ|научиться отказывать|ассертивн|говорить нет", re.IGNORECASE)
RELATION_RX = re.compile(r"отношен|конфликт|ссор|партнер|муж|жена|коллег", re.IGNORECASE)
IMPOSTER_RX = re.compile(r"самозван|не достойн|недостаточн.*хорош", re.IGNORECASE)
BURNOUT_RX = re.compile(r"выгора|усталость хронич|опустошен", re.IGNORECASE)
SLEEP_RX = re.compile(r"сон|бессонниц|режим сна", re.IGNORECASE)
MOTIV_RX = re.compile(r"мотивац|нет сил|не хочется", re.IGNORECASE)
ANGER_RX = re.compile(r"злость|ярость|злюсь|бесит", re.IGNORECASE)
SAD_RX = re.compile(r"груст|печаль|потеря|скорбь", re.IGNORECASE)
MINDFUL_RX = re.compile(r"майндфул|осознанн|дыхани|медитац", re.IGNORECASE)
CBT_RX = re.compile(r"рефрейм|когнитивн|автоматическ.*мысл", re.IGNORECASE)
SMART_RX = re.compile(r"smart|смарт", re.IGNORECASE)
EISEN_RX = re.compile(r"эйзенхау|важно-срочн|матриц", re.IGNORECASE)
POMODORO_RX = re.compile(r"помодор|тайм[- ]?бокс|time[- ]?box", re.IGNORECASE)

FINANCE_RX = re.compile(r"(деньг|финанс|доход|расход|бюджет|подушк|долг|кредит|ипотек|копит|не хватает|денежн.*тревог)", re.IGNORECASE)

PARTNER_RX = re.compile(r"(найти|поиск|встретить).*(партн|муж|жен|парня|девушк)", re.IGNORECASE)
CAREER_RX = re.compile(r"(карь|повышен|рост|развитие|зарплат|оценк).*работ", re.IGNORECASE)
SPEAK_RX = re.compile(r"(выступлен|презентац|публичн.*выступ|самопрезент)", re.IGNORECASE)
NEGOT_RX = re.compile(r"(переговор|торг|обсужд.*услов|договор)", re.IGNORECASE)
INTERVIEW_RX = re.compile(r"(собеседован|интервью|hr|рекрутер)", re.IGNORECASE)
WEEKLY_RX = re.compile(r"(еженедел|обзор|ретросп|review)", re.IGNORECASE)
STUDY_RX = re.compile(r"(учеб|экзам|курс|диплом|учит|школ|универ)", re.IGNORECASE)
ADHD_RX = re.compile(r"(adhd|сдвр|рассеянн|невниман|гиперактив)", re.IGNORECASE)
DECLUTTER_RX = re.compile(r"(расхлам|разбор.*вещ|уборк|минимализм)", re.IGNORECASE)
PARENT_RX = re.compile(r"(ребен|детьм|родительств|подрост|воспитан|моего сына|мою дочь)", re.IGNORECASE)
HABITS_RX = re.compile(r"(привычк|спорт|питани|вода|здоров|шаги)", re.IGNORECASE)
CREATIVE_RX = re.compile(r"(творческ|креативн|писател|муз|идеи.*не ид|застой)", re.IGNORECASE)
RELOC_RX = re.compile(r"(переезд|релокац|смена стран|город|адаптац)", re.IGNORECASE)
GRAT_RX = re.compile(r"(благодарност|журнал благодарност|gratitude)", re.IGNORECASE)
MORNING_RX = re.compile(r"(утренн.*ритуал|morning routine|утро.*начать)", re.IGNORECASE)


def reply_decision(style: Dict[str, str], humor_on: bool) -> str:
    lines = [
        "Давай сделаем выбор легче. 4 коротких инструмента:",
        "1) 10-10-10: что будет через 10 минут, 10 недель и 10 месяцев, если так поступишь?",
        "2) Таблица 3×3: плюсы / минусы / ценности. Что поддерживает твои ценности — то и берём.",
        "3) Шкалирование (0–10): насколько важно? Что поднимет оценку на +1 сегодня?",
        "4) Мини-эксперимент: шаг на 15 минут, чтобы проверить гипотезу на практике.",
        f"\n{('Чуть иронии: ' + playful_oneline()) if humor_on else ''}",
        "\nКакой инструмент откликается? Могу помочь применить его на твоём примере.",
    ]
    return "\n".join(lines)


def reply_stress(style: Dict[str, str], humor: bool) -> str:
    return (
        "План анти-стресса за 5 минут:\n"
        "• 30–60 сек дыхание 4-7-8 — 4 цикла.\n"
        "• Заземление 5-4-3-2-1: 5 вижу, 4 ощущаю, 3 слышу, 2 пахнет, 1 вкус.\n"
        "• Сигналы безопасности телу: расправь плечи, расслабь челюсть, вода.\n"
        "• Один микрошаг на 10 минут.\n"
        f"\n{playful_oneline() if humor else ''}\n"
        f"{'Что из этого попробуешь сейчас?' if style['plan']=='план' else 'С чего начнём — дыхание или микрошаг?'}"
    )


def reply_procras(style: Dict[str, str], humor: bool) -> str:
    return (
        "Чтобы сдвинуть прокрастинацию:\n"
        "1) Правило 2 минут — начни с действия на 120 секунд.\n"
        "2) Time-boxing 25/5 — один помидор: 25 фокус, 5 — отдых.\n"
        "3) Формула задачи: Глагол + Объект + 25 минут.\n"
        "4) «Смешно маленький шаг»: открыть файл и написать одну строку.\n"
        f"\n{playful_oneline() if humor else ''}\nКакой микрошаг берём на 10 минут?"
    )


def reply_goals(style: Dict[str, str], humor: bool) -> str:
    return (
        "Сформируем ясность:\n"
        "• SMART  • Эйзенхауэр  • Следующий видимый шаг  • Критерий завершения.\n"
        f"\n{playful_oneline() if humor else ''}\nС какой целью начнём? Опишешь в 1–2 предложениях?"
    )


def reply_boundaries(style: Dict[str, str], humor: bool) -> str:
    return (
        "Скрипт границ (Я-сообщение): Факт → Чувство → Потребность → Просьба.\n"
        "Пример: «Когда задача приходит в последний момент, я напрягаюсь; мне важно планирование, поэтому отвечу завтра к 12:00».\n"
        "Опиши свою ситуацию — соберём фразу."
    )


def reply_relation(style: Dict[str, str], humor: bool) -> str:
    return (
        "Разговор без ссор (NVC + loop-listening):\n"
        "1) Наблюдение  2) Чувства  3) Потребности  4) Просьба. Сначала коротко отражаешь мысль партнёра — потом говоришь свою.\n"
        "Опиши контекст — предложу формулировку."
    )


def reply_imposter(style: Dict[str, str], humor: bool) -> str:
    return (
        "Синдром самозванца — признак роста. Делаем «реестр доказательств»: 3 факта силы, 3 зоны развития, 1 микрошаг на обучение (15 минут).\n"
        "Приём «что бы я сказал(а) другу?» — перенеси этот тон себе."
    )


def reply_burnout(style: Dict[str, str], humor: bool) -> str:
    return (
        "Детокс выгорания:\n"
        "• 3Р: ресурс (сон/еда/движение), ритм (перерывы), радость (ежедневно).\n"
        "• Убери 2–3 энергожора, поставь верхний предел дня.\n"
        "С чего начнём — ресурс, ритм или радость?"
    )


def reply_sleep(style: Dict[str, str], humor: bool) -> str:
    return (
        "Гигиена сна:\n"
        "1) Фиксированное время подъёма  2) Минус экраны за 90 минут до сна\n"
        "3) Кофеин до 14:00  4) Если не спится 20 минут — встать, тихое занятие, вернуться при сонливости.\n"
        "Какой пункт попробуешь 3 вечера?"
    )


def reply_motiv(style: Dict[str, str], humor: bool) -> str:
    return "Возвращаем мотивацию: Зачем-слой → Порог 15 минут → Убрать трение → Ритуал старта.\nС какого шага начнём прямо сейчас?"


def reply_anger(style: Dict[str, str], humor: bool) -> str:
    return "Работа со злостью:\n1) Телесный выпуск  2) Что за граница/ценность?  3) Мягко восстановить границу.\nНужно — соберём фразу."


def reply_sad(style: Dict[str, str], humor: bool) -> str:
    return "С грустью бережно: назвать чувство (0–10) → 10 минут «побыть» → поддержать тело → один контакт с миром.\nЯ рядом. Что было бы самым бережным сейчас?"


def reply_mindful(style: Dict[str, str], humor: bool) -> str:
    return "Осознанность 2 минуты: скан тела (ступни→лицо), 10 спокойных выдохов, считай только выдохи.\nГотов(а) попробовать? Напомню про «10 выдохов» позже."


def reply_cbt(style: Dict[str, str], humor: bool) -> str:
    return (
        "Мини-лист мыслей (КПТ): Ситуация → Авто-мысль → Эмоция (0–10) → Доказательства за/против → Альтернативная мысль.\n"
        "Опиши 1–2 строки — пройдём шаги."
    )


def reply_smart(style: Dict[str, str], humor: bool) -> str:
    return "Оформим цель по SMART: «До [дата] я [результат]; измерю по [метрика]; важно потому что [значимость]».\nКинь черновик — отточим."


def reply_eisen(style: Dict[str, str], humor: bool) -> str:
    return "Матрица Эйзенхауэра: I — сегодня; II — план; III — делегирую; IV — убираю.\nДавай раскидаем 5 твоих задач по квадрантам."


def reply_pomodoro(style: Dict[str, str], humor: bool) -> str:
    return "Помидор: 25 фокус + 5 пауза × 4 → длинная пауза. На цикл — одна мини-цель. Какую возьмём?"


def reply_finance(style: Dict[str, str], humor: bool) -> str:
    return (
        "Денежная тревога — спокойно и по делу. План 20–30 минут:\n"
        "1) 5 выдохов + вода  2) Снимок: доход/расход/долги/подушка  3) Три рычага: урезать, подзаработать, копить  4) Микрошаг сегодня (15 мин).\n"
        f"{'Бонус — немного иронии: ' + playful_oneline() if humor else ''}\n"
        "С чего начнём? Могу дать простой шаблон бюджета."
    )


def reply_partner(style: Dict[str, str], humor: bool) -> str:
    return (
        "Поиск партнёра:\n"
        "1) Ясность: 3 обязательных качества, 3 желательных, 3 «красных флага».\n"
        "2) Среды: 2–3 места/активности, где такие люди бывают.\n"
        "3) Скрипт лёгкого контакта + открытый вопрос.\n"
        "4) Ритм: одно социальное действие в день.\n"
        "С чего начнём на этой неделе?"
    )


def reply_career(style: Dict[str, str], humor: bool) -> str:
    return (
        "Карьерный апгрейд:\n"
        "• Карта ценности (3 результата за 6–12 мес)  • Гэп-анализ навыков\n"
        "• Разговор о росте: наблюдение → ценность для компании → предложение шага\n"
        "• Рынок: 2 отклика в неделю + 1 тёплое знакомство.\n"
        "Какой шаг берём на 7 дней?"
    )


def reply_speaking(style: Dict[str, str], humor: bool) -> str:
    return "Выступление:\n1 идея → 3 пункта → 1 история на пункт. Слайды — опоры, не текст. 2 репетиции по таймеру + запись голоса.\nНапишем тезисы?"


def reply_negotiation(style: Dict[str, str], humor: bool) -> str:
    return "Переговоры (интересы → варианты → критерии). Скрипт: «Хочу договориться так, чтобы обеим сторонам было хорошо. Что для вас самое важное?»\nОпиши кейс — соберём план."


def reply_interview(style: Dict[str, str], humor: bool) -> str:
    return "Собеседование: 3 истории по STAR, питч 60–90 сек, вопросы к компании и письмо-резюме после.\nНабросаем 1 историю?"


def reply_weekly(style: Dict[str, str], humor: bool) -> str:
    return "Еженедельный обзор: инбокс-ноль → 3 сделанных/3 урока/1 радость → 3 приоритета недели → бронь в календаре → один ритуал заботы.\nНужен чек-лист?"


def reply_study(style: Dict[str, str], humor: bool) -> str:
    return "Учёба: помидоры 25/5, карта тем, метод Фейнмана, интервальные повторы (сегодня/завтра/3 дня/неделя).\nКакая тема сейчас?"


def reply_adhd(style: Dict[str, str], humor: bool) -> str:
    return "АДХД-дружественный режим: таймер, визуальный список, тёплый старт 5 минут, правило 80%, быстрые награды.\nЧто попробуем первым?"


def reply_declutter(style: Dict[str, str], humor: bool) -> str:
    return "Расхламление 20 минут: одна зона → таймер → оставить/отдать/выкинуть/карантин 30 дней → фото «после». С какой зоны начнём?"


def reply_parent(style: Dict[str, str], humor: bool) -> str:
    return "Поддерживающее родительство: заметить хорошее (факт), выбор из двух хороших, валидировать эмоцию — потом границы и план.\nОпиши момент — предложу формулировку."


def reply_habits(style: Dict[str, str], humor: bool) -> str:
    return "Привычки 1%: привязка к триггеру, мини-версия 2 минуты, счётчик дней, награда. Что берём на 7 дней?"


def reply_creative(style: Dict[str, str], humor: bool) -> str:
    return "Творческая разморозка: «плохой черновик» 15 минут, ограничение (6 строк/3 цвета), смена среды. Что черкнём сейчас?"


def reply_reloc(style: Dict[str, str], humor: bool) -> str:
    return "Переезд: быт (список на неделю) • люди (1 инициатива в неделю) • домашние ритуалы. Что добавим в «недельную карту»?"


def reply_grat(style: Dict[str, str], humor: bool) -> str:
    return "Дневник благодарности 3×3: 3 факта за сегодня, 3 качества в себе, 3 маленьких радости. Запишем первую тройку?"


def reply_morning(style: Dict[str, str], humor: bool) -> str:
    return "Утренний ритуал 10–15 мин: вода+свет → 10 выдохов → план 3 приоритета → 2 мин движений → доброе намерение. Сделать карточку-памятку?"


INTENTS: List[Tuple[re.Pattern, IntentFn, str]] = [
    (DECISION_RX, reply_decision, "decision"),
    (STRESS_RX, reply_stress, "stress"),
    (PROCRAS_RX, reply_procras, "procrastination"),
    (GOALS_RX, reply_goals, "goals"),
    (BOUNDARY_RX, reply_boundaries, "boundaries"),
    (RELATION_RX, reply_relation, "relations"),
    (IMPOSTER_RX, reply_imposter, "imposter"),
    (BURNOUT_RX, reply_burnout, "burnout"),
    (SLEEP_RX, reply_sleep, "sleep"),
    (MOTIV_RX, reply_motiv, "motivation"),
    (ANGER_RX, reply_anger, "anger"),
    (SAD_RX, reply_sad, "sadness"),
    (MINDFUL_RX, reply_mindful, "mindfulness"),
    (CBT_RX, reply_cbt, "cbt"),
    (SMART_RX, reply_smart, "smart"),
    (EISEN_RX, reply_eisen, "eisenhower"),
    (POMODORO_RX, reply_pomodoro, "pomodoro"),
    (FINANCE_RX, reply_finance, "finance_anxiety"),
    (PARTNER_RX, reply_partner, "partner_search"),
    (CAREER_RX, reply_career, "career"),
    (SPEAK_RX, reply_speaking, "public_speaking"),
    (NEGOT_RX, reply_negotiation, "negotiations"),
    (INTERVIEW_RX, reply_interview, "interview"),
    (WEEKLY_RX, reply_weekly, "weekly_review"),
    (STUDY_RX, reply_study, "study"),
    (ADHD_RX, reply_adhd, "adhd_mode"),
    (DECLUTTER_RX, reply_declutter, "declutter"),
    (PARENT_RX, reply_parent, "parenting"),
    (HABITS_RX, reply_habits, "healthy_habits"),
    (CREATIVE_RX, reply_creative, "creative_block"),
    (RELOC_RX, reply_reloc, "relocation"),
    (GRAT_RX, reply_grat, "gratitude"),
    (MORNING_RX, reply_morning, "morning_routine"),
]

CODE2FN: Dict[str, IntentFn] = {code: fn for (_rx, fn, code) in INTENTS}

MENU_TRIGGERS = re.compile(r"\b(по какой теме|какая тема|меню|непонятно|что выбрать|где здесь)\b", re.IGNORECASE)

MENU_LIST: List[Tuple[str, str]] = [
    ("decision", "Принять решение"),
    ("stress", "Снизить стресс/тревогу"),
    ("procrastination", "Побороть прокрастинацию"),
    ("goals", "Навести ясность и цели"),
    ("finance_anxiety", "Денежная тревога/бюджет"),
    ("relations", "Отношения/конфликты"),
    ("boundaries", "Границы и «говорить нет»"),
    ("career", "Карьера/повышение"),
    ("partner_search", "Поиск партнёра"),
    ("public_speaking", "Подготовка к выступлению"),
    ("interview", "Собеседование"),
    ("negotiations", "Переговоры"),
    ("weekly_review", "Еженедельный обзор"),
    ("study", "Учёба/экзамены"),
    ("adhd_mode", "Фокус-режим (АДХД-дружественный)"),
    ("declutter", "Расхламление"),
    ("healthy_habits", "Полезные привычки"),
    ("creative_block", "Творческий застой"),
    ("relocation", "Переезд/перемены"),
    ("gratitude", "Дневник благодарности"),
    ("morning_routine", "Утренний ритуал"),
]


async def compose_menu(uid: int) -> str:
    mapping = {str(i + 1): code for i, (code, _title) in enumerate(MENU_LIST[:10])}
    await set_state(uid, {"menu_map": mapping})
    lines = ["Выбери тему цифрой, а я сразу предложу план:\n"]
    for i, (_code, title) in enumerate(MENU_LIST[:10], start=1):
        lines.append(f"{i}) {title}")
    lines.append("\nМожно написать свою тему словами — я пойму.")
    return "\n".join(lines)


async def try_menu_choice(uid: int, text: str, style: Dict[str, str], humor_on: bool) -> Optional[str]:
    st = await app_state(uid)
    mapping = st.get("menu_map") or {}
    t = (text or "").strip()
    if t in mapping:
        code = mapping[t]
        fn = CODE2FN.get(code)
        if fn:
            await set_state(uid, {"menu_map": {}})
            return fn(style, humor_on)
    return None


def focus_question(style: Dict[str, str]) -> str:
    return "Что здесь для тебя главное?" if style["detail"] == "смыслы" else "Какие конкретные шаги ты видишь здесь?"


def step_question(style: Dict[str, str]) -> str:
    return "Какой маленький шаг ты готова наметить на сегодня?" if style["plan"] == "план" else "Какой лёгкий эксперимент попробуешь сначала?"


async def build_reply(uid: int, user_text: str, humor_on: bool) -> str:
    pr = await _fetch("SELECT ei,sn,tf,jp,mbti_type FROM psycho_profile WHERE user_id=$1", uid)
    p = pr[0] if pr else {"ei": 0.5, "sn": 0.5, "tf": 0.5, "jp": 0.5}
    st = comms_style(p)
    t = (user_text or "").strip()

    if MENU_TRIGGERS.search(t):
        return await compose_menu(uid)

    if re.search(r"\bпошути\b|немного юмора|чуть иронии", t.lower()):
        return playful_oneline() + "\n\n" + focus_question(st)

    for rx, fn, _code in INTENTS:
        if rx.search(t):
            return fn(st, humor_on)

    if t.endswith("?") or re.search(r"\b(как|что|зачем|почему|какой|какая|когда)\b", t.lower()):
        return f"{reflect_emotion(t)}Попробую по делу. {focus_question(st)}\n\n{step_question(st)}"

    if len(t) < 4:
        return await compose_menu(uid)

    return (
        f"{reflect_emotion(t)}Чтобы продвинуться по теме — выдели 5–10 минут и выпиши 3 шага/мысли. "
        f"Какой из них попробуешь сегодня? Если хочется — скажи «пошути», добавлю лёгкой иронии. "
        f"Или выбери тему цифрой:\n{await compose_menu(uid)}"
    )


async def not_duplicate(uid: int, reply: str) -> str:
    last = await _fetch(
        "SELECT text FROM dialog_events WHERE user_id=$1 AND role='assistant' ORDER BY id DESC LIMIT 1",
        uid,
    )
    if last and (last[0].get("text") or "").strip() == reply.strip():
        return reply + "\n\nЕсли хочется, посмотрим на это под другим углом 😉"
    return reply


# -------------------- API --------------------
@app.get("/")
async def root() -> Dict[str, Any]:
    return {"ok": True, "service": "anima"}


@app.post("/webhook/telegram")
async def webhook(update: TelegramUpdate, request: Request) -> Dict[str, Any]:
    # webhook secret (header-based)
    if WEBHOOK_SECRET:
        got = request.headers.get("X-Webhook-Secret", "")
        if got != WEBHOOK_SECRET:
            logger.warning("Webhook forbidden: bad secret. ip=%s", request.client.host if request.client else None)
            raise HTTPException(status_code=401, detail="Unauthorized")
    else:
        logger.warning("WEBHOOK_SECRET is not set. Webhook endpoint is not protected.")

    # idempotency
    if update.update_id is not None:
        try:
            inserted = await mark_update_processed(int(update.update_id))
            if not inserted:
                return {"ok": True}
        except Exception:
            logger.exception("Idempotency check failed (update_id=%s)", update.update_id)
            # If DB fails, do not process duplicates blindly:
            raise HTTPException(status_code=503, detail="DB unavailable")

    if not update.message:
        return {"ok": True}

    msg = update.message
    chat_id = int(msg["chat"]["id"])
    uid = chat_id
    text = (msg.get("text") or "").strip()

    u = msg.get("from", {}) or {}
    try:
        await ensure_user(uid, u.get("username"), u.get("first_name"), u.get("last_name"))
    except Exception:
        logger.exception("ensure_user failed (uid=%s)", uid)

    logger.info("telegram_update chat_id=%s text_len=%s", chat_id, len(text))

    # toggles
    if text.lower().startswith("/humor"):
        on = any(w in text.lower() for w in ["on", "вкл", "да", "true"])
        st = await app_state(uid)
        st["humor_on"] = on
        await set_state(uid, st)
        await tg_send(chat_id, "Юмор включён 😊" if on else "Юмор выключен 👍")
        return {"ok": True}

    st = await app_state(uid)
    if re.search(r"\bпошути\b|немного юмора|чуть иронии", text.lower()):
        st["humor_on"] = True
        await set_state(uid, st)

    # Safety
    if crisis_detect(text):
        reply = (
            "Я рядом и слышу твою боль. Если нужна поддержка прямо сейчас — "
            "обратись к близким или в службу помощи. "
            "Что сейчас было бы самым бережным для тебя?"
        )
        await tg_send(chat_id, reply)
        await _execute(
            "INSERT INTO dialog_events(user_id,role,text,mi_phase,emotion,relevance) VALUES($1,'assistant',$2,'support','tense',false)",
            uid,
            reply,
        )
        return {"ok": True}

    if STOP.search(text):
        reply = "Давай оставим чувствительные темы за рамками. О чём тебе важнее поговорить сейчас?"
        await tg_send(chat_id, reply)
        await _execute(
            "INSERT INTO dialog_events(user_id,role,text,mi_phase,emotion,relevance) VALUES($1,'assistant',$2,'engage','neutral',false)",
            uid,
            reply,
        )
        return {"ok": True}

    # Greeting & name
    name = st.get("name")
    intro_done = bool(st.get("intro_done", False))

    if text.lower() in ("/start", "start"):
        await set_state(uid, {"intro_done": False, "name": None, "kno_idx": None, "kno_done": False, "menu_map": {}})
        greet = (
            "Привет 🌿 Я Анима — твой личный психологический ассистент. "
            "Я помогаю навести ясность, снизить стресс и наметить шаги вперёд. "
            "Наши разговоры конфиденциальны, никакого спама — только поддержка 💛\n\n"
            "Как мне к тебе обращаться?"
        )
        await tg_send(chat_id, greet)
        await _execute("INSERT INTO dialog_events(user_id,role,text,mi_phase) VALUES($1,'assistant',$2,'engage')", uid, greet)
        return {"ok": True}

    if not intro_done:
        if not name:
            if len(text) <= 40 and not re.search(r"\d", text):
                await set_state(uid, {"name": text})
                prompt = "Как ты сейчас? Выбери слово: спокойно, напряжённо, растерянно — или опиши по-своему."
                await tg_send(chat_id, f"Рада знакомству, {text}! ✨")
                await tg_send(chat_id, prompt)
                return {"ok": True}
            await tg_send(chat_id, "Как мне к тебе обращаться? Коротко — одним словом 🙂")
            return {"ok": True}

        await set_state(uid, {"intro_done": True})
        await tg_send(chat_id, "Спасибо! Начнём с короткой анкеты (6 вопросов). Отвечай 1 или 2, можно словами.")
        await kno_start(uid)
        nxt = await kno_next(uid)
        if nxt:
            await tg_send(chat_id, nxt)
        return {"ok": True}

    # KNO flow
    st = await app_state(uid)
    if not st.get("kno_done"):
        nxt = await kno_register(uid, text)
        if nxt is None:
            prof = (await _fetch("SELECT ei,sn,tf,jp,confidence FROM psycho_profile WHERE user_id=$1", uid))[0]
            conf = int((prof.get("confidence") or 0) * 100)
            summary = (
                "Спасибо, я лучше понимаю, как с тобой говорить 💛\n"
                f"Уверенность {conf}%\n"
                "Пока это черновой профиль. Он будет уточняться по ходу диалога.\n\n"
                "Расскажи коротко — с чем хочешь сегодня поработать или о чём поговорить?\n\n"
                + (await compose_menu(uid))
            )
            await tg_send(chat_id, summary)
            await _execute("INSERT INTO dialog_events(user_id,role,text,mi_phase) VALUES($1,'assistant',$2,'engage')", uid, summary)
            return {"ok": True}

        await tg_send(chat_id, nxt)
        await _execute("INSERT INTO dialog_events(user_id,role,text,mi_phase) VALUES($1,'assistant',$2,'engage')", uid, nxt)
        return {"ok": True}

    # ---------- Free dialogue ----------
    emo = detect_emotion(text)
    humor_on = bool(st.get("humor_on"))

    pr = await _fetch("SELECT ei,sn,tf,jp FROM psycho_profile WHERE user_id=$1", uid)
    p = pr[0] if pr else {"ei": 0.5, "sn": 0.5, "tf": 0.5, "jp": 0.5}
    style = comms_style(p)

    menu_choice = await try_menu_choice(uid, text, style, humor_on)
    if menu_choice:
        draft = menu_choice
    else:
        draft = await build_reply(uid, text, humor_on)

    if quality_score(text, draft) < 0.55:
        draft = await compose_menu(uid)

    draft = await not_duplicate(uid, draft)

    await tg_send(chat_id, draft)

    await _execute(
        "INSERT INTO dialog_events(user_id,role,text,mi_phase,emotion,relevance) VALUES($1,'user',$2,'engage',$3,true)",
        uid,
        text,
        emo,
    )
    await _execute(
        "INSERT INTO dialog_events(user_id,role,text,mi_phase,emotion,relevance) VALUES($1,'assistant',$2,'engage',$3,true)",
        uid,
        draft,
        emo,
    )

    return {"ok": True}

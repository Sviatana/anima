# api/main.py
import os, re, json, time
from typing import Any, Dict, Optional, List, Tuple, Callable
from fastapi import FastAPI, Request, Header
from pydantic import BaseModel
from dotenv import load_dotenv
import httpx
import psycopg2, psycopg2.extras

# -------------------- init --------------------
load_dotenv()
app = FastAPI(title="ANIMA 2.0")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
DB_URL         = os.getenv("DATABASE_URL", "")
REPORTS_TOKEN  = os.getenv("REPORTS_TOKEN", "")

# -------------------- DB helpers --------------------
def db():
    return psycopg2.connect(DB_URL)

def q(query: str, params: Tuple = ()):
    conn = db()
    try:
        with conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(query, params)
                if cur.description:
                    return cur.fetchall()
                return None
    finally:
        conn.close()

# one-time DDL
q("""
CREATE TABLE IF NOT EXISTS processed_updates(
  update_id BIGINT PRIMARY KEY,
  created_at TIMESTAMPTZ DEFAULT NOW()
)
""")
q("""
CREATE TABLE IF NOT EXISTS user_profile(
  user_id BIGINT PRIMARY KEY,
  username TEXT,
  first_name TEXT,
  last_name TEXT,
  locale TEXT,
  facts JSONB DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
)
""")
q("""
CREATE TABLE IF NOT EXISTS psycho_profile(
  user_id BIGINT PRIMARY KEY REFERENCES user_profile(user_id) ON DELETE CASCADE,
  ei FLOAT DEFAULT 0.5,
  sn FLOAT DEFAULT 0.5,
  tf FLOAT DEFAULT 0.5,
  jp FLOAT DEFAULT 0.5,
  confidence FLOAT DEFAULT 0.3,
  mbti_type TEXT,
  anchors JSONB DEFAULT '[]'::jsonb,
  state TEXT,
  updated_at TIMESTAMPTZ DEFAULT NOW()
)
""")
q("""
CREATE TABLE IF NOT EXISTS dialog_events(
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT REFERENCES user_profile(user_id) ON DELETE CASCADE,
  role TEXT CHECK (role IN ('user','assistant','system')),
  text TEXT,
  emotion TEXT,
  mi_phase TEXT,
  topic TEXT,
  relevance BOOLEAN,
  axes JSONB,
  created_at TIMESTAMPTZ DEFAULT NOW()
)
""")

# -------------------- Telegram --------------------
class TelegramUpdate(BaseModel):
    update_id: Optional[int] = None
    message: Optional[Dict[str, Any]] = None

async def tg_send(chat_id: int, text: str):
    if not TELEGRAM_TOKEN:
        print(f"[DRY RUN] -> {chat_id}: {text}")
        return
    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text},
        )

# -------------------- Safety & evaluator --------------------
STOP = re.compile(r"(политик|религ|насили|медицинск|вакцин|диагноз|лекарств|суицид)", re.IGNORECASE)
CRISIS = re.compile(r"(не хочу жить|самоповрежд|отчаяни|суицид|покончи|боль невыносима)", re.IGNORECASE)

def crisis_detect(t: str) -> bool:
    return bool(CRISIS.search(t or ""))

def detect_emotion(t: str) -> str:
    tl = (t or "").lower()
    if re.search(r"устал|напряж|тревож|страш|злюсь|злость|раздраж|грустн|плохо|паник",tl): return "tense"
    if re.search(r"спокойн|рад|легко|хорошо|класс|радост",tl): return "calm"
    if re.search(r"не знаю|путаюсь|сомнева|непонятно|не понимаю|затрудня",tl): return "uncertain"
    return "neutral"

def quality_score(user_text: str, reply: str) -> float:
    s = 0.0
    L = len(reply or "")
    if 80 <= L <= 700: s += 0.25
    if "?" in (reply or ""): s += 0.2
    if re.search(r"(слышу|вижу|понимаю|рядом|важно|чувствую)", (reply or "").lower()):
        s += 0.25
    tokens = [w for w in re.findall(r"[а-яa-z]{4,}", (user_text or "").lower()) if w not in {"сейчас","просто","очень","хочу"}]
    if any(t in (reply or "").lower() for t in tokens[:6]): s += 0.3
    return s

# -------------------- Onboarding KNO --------------------
KNO = [
    ("ei_q1", "Когда ты устаёшь — что помогает быстрее восстановиться: пообщаться с людьми 🪴 или побыть наедине ☁️?"),
    ("sn_q1", "Что тебе ближе: действовать по конкретным шагам и фактам 🎯 или ориентироваться на идею и смысл ✨?"),
    ("tf_q1", "Как ты чаще принимаешь решения: через логику и аргументы 🧠 или через чувства и внутренние ценности 💛?"),
    ("jp_q1", "Когда тебе спокойнее: когда всё чётко спланировано 📋 или когда есть свобода и импровизация 🎯?"),
    ("jp_q2", "Когда много задач: составить список заранее или пробовать и смотреть по ситуации?"),
    ("ei_q2", "Когда нужно разобраться: поговорить с кем-то или записать мысли для себя?")
]
KNO_MAP = {"ei_q1":("E","I"), "sn_q1":("S","N"), "tf_q1":("T","F"), "jp_q1":("J","P"), "jp_q2":("J","P"), "ei_q2":("E","I")}

def ensure_user(uid:int, username=None, first_name=None, last_name=None):
    q("""INSERT INTO user_profile(user_id,username,first_name,last_name)
         VALUES(%s,%s,%s,%s)
         ON CONFLICT (user_id) DO NOTHING""",
      (uid,username,first_name,last_name))

def get_facts(uid:int)->Dict[str,Any]:
    r = q("SELECT facts FROM user_profile WHERE user_id=%s",(uid,))
    return r[0]["facts"] if r and r[0]["facts"] else {}

def set_facts(uid:int, patch:Dict[str,Any]):
    facts = get_facts(uid)
    facts.update(patch)
    q("UPDATE user_profile SET facts=%s, updated_at=NOW() WHERE user_id=%s",(json.dumps(facts),uid))

def app_state(uid:int)->Dict[str,Any]:
    return get_facts(uid).get("app_state",{})

def set_state(uid:int, patch:Dict[str,Any]):
    facts = get_facts(uid)
    st = facts.get("app_state",{})
    st.update(patch)
    facts["app_state"] = st
    q("UPDATE user_profile SET facts=%s, updated_at=NOW() WHERE user_id=%s",(json.dumps(facts),uid))

def kno_start(uid:int):
    set_state(uid, {"kno_idx":0, "kno_answers":{}, "kno_done":False})

def kno_next(uid:int)->Optional[str]:
    st = app_state(uid)
    idx = st.get("kno_idx", 0)
    if idx is None: return None
    if idx >= len(KNO): return None
    return KNO[idx][1] + "\n\nОтветь 1 или 2, можно словами."

def kno_register(uid:int, text:str)->Optional[str]:
    st = app_state(uid)
    idx = st.get("kno_idx", 0)
    if idx is None or idx >= len(KNO): return None

    key,_ = KNO[idx]
    t = (text or "").strip().lower()
    def pick(question_key:str, t:str)->int:
        if t in {"1","первый","первое","первая","слева"}: return 1
        if t in {"2","второй","второе","вторая","справа"}: return 2
        if question_key.startswith("ei_"):
            if re.search(r"наедин|тишин|один", t): return 2
            if re.search(r"люд|общат|встреч", t):  return 1
        if question_key.startswith("sn_"):
            if re.search(r"факт|конкрет|шаг", t): return 1
            if re.search(r"смысл|иде|образ", t):   return 2
        if question_key.startswith("tf_"):
            if re.search(r"логик|рацион|аргумент", t): return 1
            if re.search(r"чувств|эмоци|ценност", t):  return 2
        if question_key.startswith("jp_"):
            if re.search(r"план|распис|контрол", t): return 1
            if re.search(r"свобод|импров|спонтан", t): return 2
        return 1

    answers = st.get("kno_answers",{})
    answers[key] = pick(key,t)

    idx += 1
    if idx >= len(KNO):
        axes = {"E":0,"I":0,"S":0,"N":0,"T":0,"F":0,"J":0,"P":0}
        for k,v in answers.items():
            a,b = KNO_MAP[k]
            axes[a if v==1 else b]+=1
        def norm(a,b): s=a+b; return (a/(s or 1), b/(s or 1))
        E,I = norm(axes["E"],axes["I"]); S,N = norm(axes["S"],axes["N"])
        T,F = norm(axes["T"],axes["F"]); J,P = norm(axes["J"],axes["P"])
        q("""INSERT INTO psycho_profile(user_id,ei,sn,tf,jp,confidence,mbti_type,anchors,state)
             VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
             ON CONFLICT (user_id) DO UPDATE
             SET ei=EXCLUDED.ei,sn=EXCLUDED.sn,tf=EXCLUDED.tf,jp=EXCLUDED.jp,
                 confidence=EXCLUDED.confidence,updated_at=NOW()""",
          (uid,E,N,T,J,0.4,None,json.dumps([]),None))
        set_state(uid, {"kno_done":True, "kno_idx":None, "kno_answers":answers})
        prof = q("SELECT ei,sn,tf,jp,confidence FROM psycho_profile WHERE user_id=%s",(uid,))[0]
        conf = int((prof["confidence"] or 0)*100)
        return ("Спасибо, я лучше понимаю, как с тобой говорить 💛\n"
                f"Уверенность {conf}%\n"
                "Пока это черновой профиль. Он будет уточняться по ходу диалога.")
    else:
        set_state(uid, {"kno_idx":idx, "kno_answers":answers})
        return KNO[idx][1] + "\n\nОтветь 1 или 2, можно словами."

# -------------------- Dialogue engine --------------------
def comms_style(p:Dict[str,Any])->Dict[str,str]:
    return {
        "tone":   "активный" if p.get("ei",0.5)>=0.5 else "спокойный",
        "detail": "смыслы"   if p.get("sn",0.5)>=0.5 else "шаги",
        "mind":   "анализ"   if p.get("tf",0.5)>=0.5 else "чувства",
        "plan":   "план"     if p.get("jp",0.5)>=0.5 else "эксперимент"
    }

def reflect_emotion(text:str)->str:
    t=(text or "").lower()
    if re.search(r"устал|напряж|тревож|злюсь|грустн|плохо|паник",t): return "Слышу напряжение и заботу о результате. "
    if re.search(r"спокойн|рад|легко|класс|хорошо",t): return "Чувствую спокойствие и лёгкость. "
    if re.search(r"не знаю|путаюсь|сомнева|непонятно",t): return "Вижу, что хочется ясности. "
    return "Я рядом и слышу тебя. "

def playful_oneline()->str:
    jokes = [
        "Иногда лучший выбор — выбрать один микрошаг. Потому что диван уже выбрал тебя 😄",
        "Если сомневаешься — выбери вариант, где ты добрее к себе. Это почти всегда выигрыш 😉",
        "Секрет продуктивности — начать. Остальное догонит 🚶‍♀️",
        "Мозг любит завершать начатое. Запусти 10 минут — и он уже за тебя 🤖"
    ]
    return jokes[int(time.time()) % len(jokes)]

# ---------- Intent handlers ----------
IntentFn = Callable[[Dict[str,str], bool], str]

DECISION_RX       = re.compile(r"(правильн|лучший).*выбор|как.*решен|принять.*решен", re.IGNORECASE)
STRESS_RX         = re.compile(r"стресс|тревог|паник|пережив|напряжен", re.IGNORECASE)
PROCRAS_RX        = re.compile(r"прокраст|не могу начать|откладыва", re.IGNORECASE)
GOALS_RX          = re.compile(r"цель|план|стратеги|куда двигаться|приоритет", re.IGNORECASE)
BOUNDARY_RX       = re.compile(r"границ|научиться отказывать|ассертивн|говорить нет", re.IGNORECASE)
RELATION_RX       = re.compile(r"отношен|конфликт|ссор|партнер|муж|жена|коллег", re.IGNORECASE)
IMPOSTER_RX       = re.compile(r"самозван|не достойн|недостаточн.*хорош", re.IGNORECASE)
BURNOUT_RX        = re.compile(r"выгора|усталость хронич|опустошен", re.IGNORECASE)
SLEEP_RX          = re.compile(r"сон|бессонниц|режим сна", re.IGNORECASE)
MOTIV_RX          = re.compile(r"мотивац|нет сил|не хочется", re.IGNORECASE)
ANGER_RX          = re.compile(r"злость|ярость|злюсь|бесит", re.IGNORECASE)
SAD_RX            = re.compile(r"груст|печаль|потеря|скорбь", re.IGNORECASE)
MINDFUL_RX        = re.compile(r"майндфул|осознанн|дыхани|медитац", re.IGNORECASE)
CBT_RX            = re.compile(r"рефрейм|когнитивн|автоматическ.*мысл", re.IGNORECASE)
SMART_RX          = re.compile(r"smart|смарт", re.IGNORECASE)
EISEN_RX          = re.compile(r"эйзенхау|важно-срочн|матриц", re.IGNORECASE)
POMODORO_RX       = re.compile(r"помодор|тайм[- ]?бокс|time[- ]?box", re.IGNORECASE)

# >>> NEW: Денежная тревога
FINANCE_RX        = re.compile(
    r"(деньг|финанс|доход|расход|бюджет|подушк|долг|кредит|ипотек|копит|не хватает|денежн.*тревог)",
    re.IGNORECASE
)

def reply_decision(style:Dict[str,str], humor_on:bool)->str:
    lines = [
        "Давай сделаем выбор легче. 4 коротких инструмента:",
        "1) **10-10-10**: что будет через 10 минут, 10 недель и 10 месяцев, если так поступишь?",
        "2) **Таблица 3×3**: плюсы / минусы / ценности. Что поддерживает твои ценности — то и берём.",
        "3) **Шкалирование (0–10)**: насколько важно? Что поднимет оценку на +1 сегодня?",
        "4) **Мини-эксперимент**: шаг на 15 минут, чтобы проверить гипотезу на практике.",
        f"\n{('Чуть иронии: ' + playful_oneline()) if humor_on else ''}",
        "\nКакой инструмент откликается? Могу помочь применить его на твоём примере."
    ]
    return "\n".join(lines)

def reply_stress(style, humor)->str:
    return (
        "План анти-стресса за 5 минут:\n"
        "• 30–60 сек **дыхание 4-7-8** (вдох-4, задержка-7, выдох-8) — 4 цикла.\n"
        "• **Заземление 5-4-3-2-1**: 5 вижу, 4 ощущаю, 3 слышу, 2 пахнет, 1 вкус.\n"
        "• Дай телу сигналы безопасности: расправь плечи, расслабь челюсть, вода небольшими глотками.\n"
        "• Определи один **микрошаг** на 10 минут — это снижает тревогу действием.\n"
        f"\n{playful_oneline() if humor else ''}\n"
        f"{'Что из этого попробуешь сейчас?' if style['plan']=='план' else 'С чего начнём — дыхание или микрошаг?'}"
    )

def reply_procras(style, humor)->str:
    return (
        "Чтобы сдвинуть прокрастинацию:\n"
        "1) **Правило 2 минут** — начни с действия, которое реально уложится в 120 секунд.\n"
        "2) **Time-boxing 25/5** — один помидор: 25 минут фокус, 5 — отдых.\n"
        "3) Уточни задачу по формуле **Глагол + Объект + 25 минут** (например: «разобрать 10 писем»).\n"
        "4) Сделай шаг смешно маленьким: «открыть файл и написать одну строчку». Мозгу легче начать.\n"
        f"\n{playful_oneline() if humor else ''}\nКакой микрошаг берём на ближайшие 10 минут?"
    )

def reply_goals(style, humor)->str:
    return (
        "Сформируем ясность:\n"
        "• **SMART**: конкретно/измеримо/достижимо/значимо/срок.\n"
        "• **Эйзенхауэр**: важное-срочное, важное-несрочное, срочное-неважное, прочее.\n"
        "• **Следующий видимый шаг**: что можно сделать за 15 минут без ожидания других?\n"
        "• **Критерий завершения**: по чему поймёшь, что задача готова?\n"
        f"\n{playful_oneline() if humor else ''}\nС какой целью начнём? Опишешь в одном-двух предложениях?"
    )

def reply_boundaries(style, humor)->str:
    return (
        "Мини-скрипты границ (формула **Я-сообщения**):\n"
        "1) Факт: «Когда …»\n2) Чувство: «я чувствую …»\n3) Потребность/План: «мне важно …, поэтому я …»\n"
        "Примеры:\n"
        "• «Когда задача приходит в последний момент, я напрягаюсь; мне важно планирование, поэтому отвечу завтра к 12:00».\n"
        "• «Я ценю наши отношения, и мне важно время на восстановление — сегодня без звонков, завтра после 11:00 смогу».\n"
        "Хочешь — подставим твою ситуацию и соберём фразу вместе."
    )

def reply_relation(style, humor)->str:
    return (
        "Алгоритм разговора без ссор (**NVC**):\n"
        "1) Наблюдение без оценки: «Когда случилось Х…»\n"
        "2) Чувства: «я чувствую …»\n3) Потребности: «мне важно …»\n4) Просьба: «можешь ли …?» (конкретно и выполнимо)\n"
        "Плюс техника **Loop-listening**: сначала дословно отражаешь ключевую мысль партнёра, потом говоришь свою.\n"
        "Опиши кратко ситуацию — предложу формулировку."
    )

def reply_imposter(style, humor)->str:
    return (
        "Синдром самозванца — нормальная реакция роста. Делаем «реестр доказательств»:\n"
        "• 3 факта компетентности (кейсы/отзывы/результаты)\n"
        "• 3 зоны развития (честно, без самокритики)\n"
        "• 1 микро-шаг на обучение (15 минут сегодня)\n"
        "И приём **Как бы я говорил другу?** — попробуй сформулировать поддержку себе в этом тоне."
    )

def reply_burnout(style, humor)->str:
    return (
        "Детокс выгорания:\n"
        "• «3Р»: ресурс (сон/еда/движение), ритм (перерывы 5–10 мин на 50–60), радость (маленькая приятность ежедневно).\n"
        "• Выдели 2–3 энергожора и 1 шаг на делегирование/отказ.\n"
        "• Поставь **верхний предел** дня (например, закончить в 19:00) — мозгу нужен конец смены.\n"
        "С чего начнём сегодня — ресурс, ритм или радость?"
    )

def reply_sleep(style, humor)->str:
    return (
        "Гигиена сна 4 шага:\n"
        "1) Фиксированное время подъёма (даже в выходные) — тело любит стабильность.\n"
        "2) 90 минут до сна — свет приглушить, экраны минимум, тёплый душ, бумажная книжка.\n"
        "3) Кофеин до 14:00, тяжёлая еда — не позднее чем за 3–4 часа.\n"
        "4) Если не спится 20 минут — встань, спокойное занятие, вернись при сонливости.\n"
        "Какой пункт возьмёшь в эксперимент на 3 вечера?"
    )

def reply_motiv(style, humor)->str:
    return (
        "Возвращаем мотивацию:\n"
        "• **Зачем-слой**: чем это служит? (деньги/свобода/интерес/люди)\n"
        "• **Доза**: снизь порог (1 задача × 15 минут)\n"
        "• **Трение**: убери лишние клики/окна, приготовь всё заранее\n"
        "• **Старт-ритуал**: одна и та же песня/чай/таймер — мозгу нужен маркер начала\n"
        "С какого шага начнём прямо сейчас?"
    )

def reply_anger(style, humor)->str:
    return (
        "Безопасная работа со злостью:\n"
        "1) Телесный выпуск: 60 секунд сильного выдоха, сжатие-расслабление кулаков, 20 приседаний.\n"
        "2) Смысл: «На что указывает злость? Где граница/ценность нарушена?»\n"
        "3) Действие: мирно восстановить границу (Я-сообщение) или переключиться.\n"
        "Нужно — соберём фразу для разговора."
    )

def reply_sad(style, humor)->str:
    return (
        "С грустью бережно:\n"
        "• Назови чувство и интенсивность 0–10.\n"
        "• Дай себе 10 минут «побыть в этом» (музыка/запись/прогулка).\n"
        "• Маленькая поддержка тела: вода, еда, тёпло.\n"
        "• Один простой контакт с миром: сообщение другу/мысль на бумагу.\n"
        "Я рядом. Что было бы самым бережным прямо сейчас?"
    )

def reply_mindful(style, humor)->str:
    return (
        "Короткая практика осознанности (2 минуты):\n"
        "• Внимание на ступни → голени → бедра (10–15 сек на область)\n"
        "• Плечи/шейя/лицо — отпусти микронатяжение\n"
        "• 10 спокойных выдохов, считая только выдохи\n"
        "Готов(а) попробовать? Я напомню про «10 выдохов» в конце беседы."
    )

def reply_cbt(style, humor)->str:
    return (
        "Сделаем мини-«лист мыслей» (КПТ):\n"
        "1) Ситуация (факты)\n2) Автоматическая мысль\n3) Эмоция (0–10)\n"
        "4) Доказательства «за» / «против» мысли\n"
        "5) Альтернативная, более точная мысль\n"
        "Опишешь 1–2 строки ситуации? Помогу пройти шаги."
    )

def reply_smart(style, humor)->str:
    return (
        "Оформим цель по **SMART**:\n"
        "S — конкретика | M — измеримость | A — реалистично | R — значимо | T — срок.\n"
        "Шаблон: «До [дата] я [глагол + результат], измерю по [метрика]. Это важно, потому что [значимость]».\n"
        "Кинь черновик — помогу отточить."
    )

def reply_eisen(style, humor)->str:
    return (
        "Матрица Эйзенхауэра:\n"
        "I. Важно-Срочно — делаю сегодня.\n"
        "II. Важно-Несрочно — планирую в календарь.\n"
        "III. Срочно-Неважно — делегирую/ограничиваю.\n"
        "IV. Неважно-Несрочно — убираю.\n"
        "Давай раскидаем 5 твоих задач по квадрантам — напиши список."
    )

def reply_pomodoro(style, humor)->str:
    return (
        "Time-boxing (Помодоро):\n"
        "• 25 минут фокуса + 5 минут пауза × 4 → длинная пауза 15–20 минут.\n"
        "• На помидор — только одна мини-цель. Ручка и блокнот для отвлекающих мыслей.\n"
        "Готов(а) на один цикл прямо сейчас? Какую мини-цель берём?"
    )

# >>> NEW: ответчик по денежной тревоге
def reply_finance(style, humor)->str:
    return (
        "Понимаю денежную тревогу — давай бережно, но по делу. Мини-план на 20–30 минут:\n"
        "1) **Снимем тревогу телом (2 мин)**: 5 глубоких выдохов, вода, расправить плечи.\n"
        "2) **Снимок финансов (10 мин, черновик)**: доход(ы)/фикс-расходы/переменные/долги/подушка.\n"
        "3) **Три рычага**:\n"
        "   • Сократить: >1–2 статьи на 30 дней (эксперимент, не наказание).\n"
        "   • Заработать: одна идея быстрых денег (подработка/час консультации/продажа вещи).\n"
        "   • Подушка: цель в месяцах × средние расходы / план пополнения.\n"
        "4) **Микрошаг сегодня (15 мин)**: написать 1 сообщение клиенту/выставить вещь на продажу/отменить ненужную подписку/сделать таблицу бюджета.\n"
        f"{'Бонус — чуть иронии: ' + playful_oneline() if humor else ''}\n"
        "С какого микрошагa начнём? Могу дать шаблон бюджета в 4 категориях."
    )

INTENTS: List[Tuple[re.Pattern, IntentFn, str]] = [
    (DECISION_RX,  reply_decision, "decision"),
    (STRESS_RX,    reply_stress,   "stress"),
    (PROCRAS_RX,   reply_procras,  "procrastination"),
    (GOALS_RX,     reply_goals,    "goals"),
    (BOUNDARY_RX,  reply_boundaries,"boundaries"),
    (RELATION_RX,  reply_relation, "relations"),
    (IMPOSTER_RX,  reply_imposter, "imposter"),
    (BURNOUT_RX,   reply_burnout,  "burnout"),
    (SLEEP_RX,     reply_sleep,    "sleep"),
    (MOTIV_RX,     reply_motiv,    "motivation"),
    (ANGER_RX,     reply_anger,    "anger"),
    (SAD_RX,       reply_sad,      "sadness"),
    (MINDFUL_RX,   reply_mindful,  "mindfulness"),
    (CBT_RX,       reply_cbt,      "cbt"),
    (SMART_RX,     reply_smart,    "smart"),
    (EISEN_RX,     reply_eisen,    "eisenhower"),
    (POMODORO_RX,  reply_pomodoro, "pomodoro"),
    # NEW:
    (FINANCE_RX,   reply_finance,  "finance_anxiety"),
]

def focus_question(style:Dict[str,str])->str:
    return "Что здесь для тебя главное?" if style["detail"]=="смыслы" else "Какие конкретные шаги ты видишь здесь?"

def step_question(style:Dict[str,str])->str:
    return "Какой маленький шаг ты готова наметить на сегодня?" if style["plan"]=="план" else "Какой лёгкий эксперимент попробуешь сначала?"

def build_reply(uid:int, user_text:str, humor_on:bool)->str:
    pr = q("SELECT ei,sn,tf,jp,mbti_type FROM psycho_profile WHERE user_id=%s",(uid,))
    p = pr[0] if pr else {"ei":0.5,"sn":0.5,"tf":0.5,"jp":0.5}
    st = comms_style(p)
    t = (user_text or "").strip()

    if re.search(r"\bпошути\b|немного юмора|чуть иронии", t.lower()):
        return playful_oneline() + "\n\n" + focus_question(st)

    for rx, fn, _code in INTENTS:
        if rx.search(t):
            return fn(st, humor_on)

    if t.endswith("?") or re.search(r"\b(как|что|зачем|почему|какой|какая)\b", t.lower()):
        return f"{reflect_emotion(t)}Попробую по делу. {focus_question(st)}\n\n{step_question(st)}"

    return (
        f"{reflect_emotion(t)}Чтобы продвинуться по теме — выдели 5–10 минут и выпиши 3 шага/мысли. "
        f"Какой из них попробуешь сегодня? Если хочется — скажи «пошути», добавлю лёгкой иронии."
    )

# -------------------- Utils --------------------
def not_duplicate(uid:int, reply:str)->str:
    last = q("SELECT text FROM dialog_events WHERE user_id=%s AND role='assistant' ORDER BY id DESC LIMIT 1",(uid,))
    if last and (last[0]["text"] or "").strip() == reply.strip():
        return reply + "\n\nЕсли хочется, посмотрим на это под другим углом 😉"
    return reply

# -------------------- API --------------------
@app.get("/")
async def root():
    return {"ok":True,"service":"anima"}

@app.post("/webhook/telegram")
async def webhook(update: TelegramUpdate, request: Request):
    if update.update_id is not None:
        already = q("SELECT 1 FROM processed_updates WHERE update_id=%s",(update.update_id,))
        if already: return {"ok":True}
        q("INSERT INTO processed_updates(update_id) VALUES(%s)",(update.update_id,))

    if not update.message:
        return {"ok":True}

    msg = update.message
    chat_id = msg["chat"]["id"]
    uid = chat_id
    text = (msg.get("text") or "").strip()
    u = msg.get("from",{})
    ensure_user(uid, u.get("username"), u.get("first_name"), u.get("last_name"))

    # humor toggle
    if text.lower().startswith("/humor"):
        on = any(w in text.lower() for w in ["on","вкл","да","true"])
        st = app_state(uid); st["humor_on"] = on; set_state(uid, st)
        await tg_send(chat_id, "Юмор включён 😊" if on else "Юмор выключен 👍")
        return {"ok":True}

    st = app_state(uid)
    if re.search(r"\bпошути\b|немного юмора|чуть иронии", text.lower()):
        st["humor_on"] = True; set_state(uid, st)

    # Safety
    if crisis_detect(text):
        reply = ("Я рядом и слышу твою боль. Если нужна поддержка прямо сейчас — "
                 "обратись к близким или в службу помощи. "
                 "Что сейчас было бы самым бережным для тебя?")
        await tg_send(chat_id, reply)
        q("INSERT INTO dialog_events(user_id,role,text,mi_phase,emotion,relevance) VALUES(%s,'assistant',%s,'support','tense',false)",(uid,reply))
        return {"ok":True}
    if STOP.search(text):
        reply = "Давай оставим чувствительные темы за рамками. О чём тебе важнее поговорить сейчас?"
        await tg_send(chat_id, reply)
        q("INSERT INTO dialog_events(user_id,role,text,mi_phase,emotion,relevance) VALUES(%s,'assistant',%s,'engage','neutral',false)",(uid,reply))
        return {"ok":True}

    # Greeting & name
    name = st.get("name")
    intro_done = st.get("intro_done", False)

    if text.lower() in ("/start","start"):
        set_state(uid, {"intro_done":False, "name":None, "kno_idx":None, "kno_done":False})
        greet = ("Привет 🌿 Я Анима — твой личный психологический ассистент. "
                 "Я помогаю навести ясность, снизить стресс и наметить шаги вперёд. "
                 "Наши разговоры конфиденциальны, никакого спама — только поддержка 💛\n\n"
                 "Как мне к тебе обращаться?")
        await tg_send(chat_id, greet)
        q("INSERT INTO dialog_events(user_id,role,text,mi_phase) VALUES(%s,'assistant',%s,'engage')",(uid,greet))
        return {"ok":True}

    if not intro_done:
        if not name:
            if len(text) <= 40 and not re.search(r"\d", text):
                set_state(uid, {"name":text})
                prompt = ("Как ты сейчас? Выбери слово: спокойно, напряжённо, растерянно — или опиши по-своему.")
                await tg_send(chat_id, f"Рада знакомству, {text}! ✨")
                await tg_send(chat_id, prompt)
                return {"ok":True}
            else:
                await tg_send(chat_id, "Как мне к тебе обращаться? Коротко — одним словом 🙂")
                return {"ok":True}
        else:
            set_state(uid, {"intro_done":True})
            await tg_send(chat_id, "Спасибо! Начнём с короткой анкеты (6 вопросов). Отвечай 1 или 2, можно словами.")
            kno_start(uid)
            nxt = kno_next(uid)
            await tg_send(chat_id, nxt)
            return {"ok":True}

    # KNO flow
    if not st.get("kno_done"):
        nxt = kno_register(uid, text)
        if nxt is None:
            prof = q("SELECT ei,sn,tf,jp,confidence FROM psycho_profile WHERE user_id=%s",(uid,))[0]
            conf = int((prof["confidence"] or 0)*100)
            summary = ("Спасибо, я лучше понимаю, как с тобой говорить 💛\n"
                       f"Уверенность {conf}%\n"
                       "Пока это черновой профиль. Он будет уточняться по ходу диалога.\n\n"
                       "Расскажи коротко — с чем хочешь сегодня поработать или о чём поговорить?")
            await tg_send(chat_id, summary)
            q("INSERT INTO dialog_events(user_id,role,text,mi_phase) VALUES(%s,'assistant',%s,'engage')",(uid,summary))
            return {"ok":True}
        else:
            await tg_send(chat_id, nxt)
            q("INSERT INTO dialog_events(user_id,role,text,mi_phase) VALUES(%s,'assistant',%s,'engage')",(uid,nxt))
            return {"ok":True}

    # ---------- Free dialogue ----------
    emo = detect_emotion(text)
    humor_on = bool(st.get("humor_on"))
    draft = build_reply(uid, text, humor_on)

    if quality_score(text, draft) < 0.55:
        draft = (f"{reflect_emotion(text)}Чтобы мне быть полезнее — скажи в одном-двух предложениях, "
                 f"что здесь для тебя главное. Затем подберём шаг на сегодня.")

    draft = not_duplicate(uid, draft)

    await tg_send(chat_id, draft)

    q("""INSERT INTO dialog_events(user_id,role,text,mi_phase,emotion,relevance)
         VALUES(%s,'user',%s,'engage',%s,true)""",(uid,text,emo))
    q("""INSERT INTO dialog_events(user_id,role,text,mi_phase,emotion,relevance)
         VALUES(%s,'assistant',%s,'engage',%s,true)""",
      (uid,draft,emo))

    return {"ok":True}

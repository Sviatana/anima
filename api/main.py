# api/main.py
import os, re, json, time, hashlib
from typing import Any, Dict, Optional, List, Tuple

from fastapi import FastAPI, Request, Header
from pydantic import BaseModel
from dotenv import load_dotenv
import httpx
import psycopg2, psycopg2.extras

# ---------------- init ----------------
load_dotenv()
app = FastAPI(title="ANIMA 2.0")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
DB_URL = os.getenv("DATABASE_URL", "")
REPORTS_TOKEN = os.getenv("REPORTS_TOKEN", "")

# ---------------- DB helpers ----------------
def db():
    return psycopg2.connect(DB_URL)

def q(query: str, params: Tuple = ()):
    conn = db()
    try:
        with conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(query, params)
                return cur.fetchall() if cur.description else None
    finally:
        conn.close()

# одноразово: таблица для идемпотентности update_id
q("""
CREATE TABLE IF NOT EXISTS processed_updates(
  update_id BIGINT PRIMARY KEY,
  created_at TIMESTAMPTZ DEFAULT NOW()
)
""")

# ---------------- Telegram ----------------
class TelegramUpdate(BaseModel):
    update_id: Optional[int] = None
    message: Optional[Dict[str, Any]] = None

async def tg_send(chat_id: int, text: str):
    if not TELEGRAM_TOKEN:
        print(f"[DRY RUN] -> {chat_id}: {text}")
        return
    async with httpx.AsyncClient(timeout=20) as client:
        await client.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text},
        )

def h(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()

def set_last_prompt(uid: int, text: str):
    st = app_state_get(uid)
    st["last_prompt_hash"] = h(text)
    app_state_set(uid, st)

def is_duplicate_prompt(uid: int, text: str) -> bool:
    st = app_state_get(uid)
    return st.get("last_prompt_hash") == h(text)

# ---------------- Safety ----------------
STOP = re.compile(r"(политик|религ|насили|медицинск|вакцин|диагноз|лекарств|суицид)", re.IGNORECASE)
CRISIS = re.compile(r"(не хочу жить|самоповрежд|отчаяни|суицид|покончи|боль невыносима)", re.IGNORECASE)

def crisis_detect(t: str) -> bool:
    return bool(CRISIS.search(t or ""))

# ---------------- Emotion ----------------
def detect_emotion(t: str) -> str:
    tl = (t or "").lower()
    if re.search(r"устал|напряж|тревож|страш|злюсь|злость|раздраж", tl): return "tense"
    if re.search(r"спокойн|рад|легко|хорошо", tl): return "calm"
    if re.search(r"не знаю|путаюсь|сомнева", tl): return "uncertain"
    return "neutral"

# ---------------- KNO (анкета) ----------------
KNO: List[Tuple[str, str]] = [
    ("ei_q1", "Когда ты устаёшь — что помогает быстрее восстановиться: пообщаться с людьми 🌱 или побыть наедине ☁️?"),
    ("sn_q1", "Что тебе ближе: действовать по конкретным шагам и фактам 🧭 или ориентироваться на идею и смысл ✨?"),
    ("tf_q1", "Как ты чаще принимаешь решения: через логику и аргументы 🧠 или через чувства и внутренние ценности 💛?"),
    ("jp_q1", "Когда тебе спокойнее: когда всё чётко спланировано 📋 или когда есть свобода и импровизация 🎯?"),
    ("jp_q2", "Когда много задач: составить список заранее или пробовать и смотреть по ситуации?"),
    ("ei_q2", "Когда нужно разобраться: поговорить с кем-то или записать мысли для себя?"),
]
KNO_MAP = {"ei_q1":("E","I"), "sn_q1":("S","N"), "tf_q1":("T","F"), "jp_q1":("J","P"), "jp_q2":("J","P"), "ei_q2":("E","I")}

# ---------------- user state ----------------
def ensure_user(uid: int, username=None, first_name=None, last_name=None):
    q("""INSERT INTO user_profile(user_id,username,first_name,last_name)
         VALUES(%s,%s,%s,%s)
         ON CONFLICT (user_id) DO NOTHING""", (uid,username,first_name,last_name))

def app_state_get(uid: int) -> Dict[str, Any]:
    r = q("SELECT facts FROM user_profile WHERE user_id=%s", (uid,))
    if not r: 
        return {}
    facts = r[0]["facts"] or {}
    if isinstance(facts, str):
        try: facts = json.loads(facts)
        except: facts = {}
    return facts.get("app_state", {}) or {}

def app_state_set(uid: int, new_state: Dict[str, Any]):
    r = q("SELECT facts FROM user_profile WHERE user_id=%s", (uid,))
    facts: Dict[str, Any] = {}
    if r and r[0]["facts"]:
        facts = r[0]["facts"]
        if isinstance(facts, str):
            try: facts = json.loads(facts)
            except: facts = {}
    facts["app_state"] = new_state
    q("UPDATE user_profile SET facts=%s, updated_at=NOW() WHERE user_id=%s", (json.dumps(facts), uid))

def app_state_patch(uid: int, patch: Dict[str, Any]):
    st = app_state_get(uid)
    st.update(patch or {})
    app_state_set(uid, st)

def kno_start(uid: int):
    app_state_patch(uid, {"kno_idx": 0, "kno_answers": {}, "kno_done": False})

def kno_step(uid: int, text: str) -> Optional[str]:
    st = app_state_get(uid)
    idx = st.get("kno_idx", 0)
    if idx is None or not isinstance(idx, int):  # страховка
        idx = 0
    answers = st.get("kno_answers", {}) or {}
    t = (text or "").strip().lower()

    def pick_by_keywords(question_key: str, t: str) -> int:
        if t in {"1","первый","первое","первая","да"}: return 1
        if t in {"2","второй","второе","вторая","нет"}: return 2
        if question_key.startswith("ei_"):
            if "наедин" in t or "один" in t or "тишин" in t: return 2
            if "люд" in t or "общат" in t or "встреч" in t: return 1
        if question_key.startswith("sn_"):
            if "факт" in t or "конкрет" in t or "шаг" in t: return 1
            if "смысл" in t or "иде" in t or "образ" in t: return 2
        if question_key.startswith("tf_"):
            if "логик" in t or "рацион" in t or "аргумент" in t: return 1
            if "чувств" in t or "эмоци" in t or "ценност" in t: return 2
        if question_key.startswith("jp_"):
            if "план" in t or "распис" in t or "контрол" in t: return 1
            if "свобод" in t or "импров" in t or "спонтан" in t: return 2
        return 1

    key, _ = KNO[idx]
    choice = pick_by_keywords(key, t)
    answers[key] = choice

    idx += 1
    if idx >= len(KNO):
        axes = {"E":0,"I":0,"S":0,"N":0,"T":0,"F":0,"J":0,"P":0}
        for k, v in answers.items():
            a,b = KNO_MAP[k]
            axes[a if v==1 else b] += 1

        def norm(a,b): s=a+b; return ((a/(s or 1)), (b/(s or 1)))
        E,I = norm(axes["E"],axes["I"]); S,N = norm(axes["S"],axes["N"])
        T,F = norm(axes["T"],axes["F"]); J,P = norm(axes["J"],axes["P"])

        # upsert профиль
        q("""
        INSERT INTO psycho_profile(user_id, ei, sn, tf, jp, confidence, mbti_type, anchors, state)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (user_id) DO UPDATE SET
          ei=EXCLUDED.ei, sn=EXCLUDED.sn, tf=EXCLUDED.tf, jp=EXCLUDED.jp,
          confidence=EXCLUDED.confidence, updated_at=NOW()
        """, (uid,E,N,T,J,0.4,None,json.dumps([]),None))

        app_state_patch(uid, {"kno_done": True, "kno_idx": None, "kno_answers": answers})
        return None
    else:
        app_state_patch(uid, {"kno_idx": idx, "kno_answers": answers})
        return KNO[idx][1] + "\n\nОтветь 1 или 2, можно словами."

# ---------------- Dialog utils ----------------
def log_event(uid: int, role: str, text: str, phase: str = "engage", emotion: Optional[str] = None):
    q("""INSERT INTO dialog_events(user_id,role,text,mi_phase,emotion)
         VALUES(%s,%s,%s,%s,%s)""", (uid, role, text, phase, emotion))

async def send_assistant(uid: int, chat_id: int, text: str, phase: str = "engage"):
    if not text: 
        return
    if not is_duplicate_prompt(uid, text):
        await tg_send(chat_id, text)
        set_last_prompt(uid, text)
        log_event(uid, "assistant", text, phase)

# ---------------- API ----------------
@app.get("/")
async def root():
    return {"ok": True, "service": "anima"}

@app.post("/webhook/telegram")
async def webhook(update: TelegramUpdate, request: Request):
    # идемпотентность
    if update.update_id is not None:
        try:
            q("INSERT INTO processed_updates(update_id) VALUES(%s) ON CONFLICT DO NOTHING", (update.update_id,))
        except Exception:
            pass  # на всякий случай

    if not update.message:
        return {"ok": True}

    msg = update.message
    chat_id = msg["chat"]["id"]
    uid = chat_id
    text = (msg.get("text") or "").strip()
    u = msg.get("from", {})
    ensure_user(uid, u.get("username"), u.get("first_name"), u.get("last_name"))

    # safety
    if crisis_detect(text):
        reply = ("Я рядом и слышу твою боль. Если нужна живая поддержка — обратись к близким "
                 "или в службу помощи. Что сейчас было бы самым поддерживающим?")
        await send_assistant(uid, chat_id, reply, "support")
        return {"ok": True}
    if STOP.search(text):
        reply = "Давай обойдём чувствительные темы. О чём важнее поговорить сейчас?"
        await send_assistant(uid, chat_id, reply, "engage")
        return {"ok": True}

    st = app_state_get(uid)

    # ---------- 1) Первое знакомство ----------
    if text.lower() in ("/start", "старт", "начать") and not st.get("intro_sent"):
        welcome = (
            "Привет 🌿\n"
            "Я — Анима, твой психологический ассистент. Помогу навести ясность, "
            "снизить стресс и найти опору.\n\n"
            "Все наши разговоры — конфиденциальны 💛\n\n"
            "Чтобы быть полезнее, предложу короткую анкету — 6 лёгких вопросов.\n"
            "Готов(-а) начать?"
        )
        app_state_patch(uid, {"intro_sent": True, "kno_idx": None, "kno_done": False})
        await send_assistant(uid, chat_id, welcome, "engage")
        return {"ok": True}

    # ---------- 2) Согласие начать анкету после приветствия ----------
    if st.get("intro_sent") and not st.get("kno_done") and st.get("kno_idx") in (None,):
        if text.lower() in {"да","давай","ок","поехали","начинай","начнем","начать"}:
            kno_start(uid)
            first = KNO[0][1] + "\n\nОтветь 1 или 2, можно своими словами."
            await send_assistant(uid, chat_id, first, "engage")
            return {"ok": True}
        else:
            hint = "Хочу убедиться, что ты готов(-а) 💛 Напиши «да» или «поехали», чтобы начать анкету."
            await send_assistant(uid, chat_id, hint, "engage")
            return {"ok": True}

    # ---------- 3) В процессе анкеты ----------
    if st.get("kno_idx") is not None and st.get("kno_done") is not True:
        nxt = kno_step(uid, text)
        if nxt is None:
            prof = q("SELECT ei,sn,tf,jp,confidence FROM psycho_profile WHERE user_id=%s", (uid,))
            conf = int(((prof[0].get("confidence") if prof else 0.4) or 0)*100)
            mbti_note = "Пока это черновой профиль. Он будет уточняться по ходу диалога."
            reply = (f"Спасибо, я лучше понимаю, как с тобой говорить 💛\n"
                     f"Уверенность {conf}%\n{mbti_note}\n\n"
                     "Расскажи коротко — с чем хочешь сегодня поработать или о чём поговорить?")
            await send_assistant(uid, chat_id, reply, "engage")
            return {"ok": True}
        else:
            await send_assistant(uid, chat_id, nxt, "engage")
            return {"ok": True}

    # ---------- 4) Обычный диалог после анкеты ----------
    emo = detect_emotion(text)
    last = q("SELECT mi_phase FROM dialog_events WHERE user_id=%s ORDER BY id DESC LIMIT 1", (uid,))
    last_phase = last[0]["mi_phase"] if last else "engage"

    # Простая персональная реплика + открытый вопрос
    if emo == "tense":
        draft = "Слышу напряжение. Давай пойдём шаг за шагом. Что здесь для тебя главное?"
    elif emo == "uncertain":
        draft = "Вижу, что хочется ясности. На чём тебе важно остановиться в первую очередь?"
    else:
        draft = "Чтобы мне быть полезнее, расскажи коротко — с чем хочешь сегодня поработать?"

    await send_assistant(uid, chat_id, draft, last_phase)
    log_event(uid, "user", text, last_phase, emo)
    return {"ok": True}

# ---------------- Reports (опц.) ----------------
def authorized(token: str) -> bool:
    return (not REPORTS_TOKEN) or token == REPORTS_TOKEN

@app.get("/reports/ping")
async def reports_ping(x_token: str = Header(default="")):
    if not authorized(x_token): return {"error": "unauthorized"}
    return {"ok": True}

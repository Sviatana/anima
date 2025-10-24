# api/main.py
import os, re, json, math, traceback
from typing import Any, Dict, Optional, List, Tuple

from fastapi import FastAPI, Request, Header
from pydantic import BaseModel
from dotenv import load_dotenv
import httpx
import psycopg2, psycopg2.extras

# -----------------------------------------------------------------------------
# Init
# -----------------------------------------------------------------------------
load_dotenv()
app = FastAPI(title="ANIMA 2.0 (v4)")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
DB_URL = os.getenv("DATABASE_URL", "")
REPORTS_TOKEN = os.getenv("REPORTS_TOKEN", "")

# -----------------------------------------------------------------------------
# DB Helpers
# -----------------------------------------------------------------------------
def db():
    return psycopg2.connect(DB_URL)

def q(query: str, params: Tuple = (), fetch: bool = True):
    conn = db()
    try:
        with conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(query, params)
                if fetch and cur.description:
                    return cur.fetchall()
    finally:
        conn.close()

def safe_exec(sql: str):
    try:
        q(sql, fetch=False)
    except Exception as e:
        print("[DB WARN]", e)

# Ensure new quality column exists
safe_exec("ALTER TABLE dialog_events ADD COLUMN IF NOT EXISTS quality JSONB;")

# -----------------------------------------------------------------------------
# Telegram
# -----------------------------------------------------------------------------
class TelegramUpdate(BaseModel):
    update_id: Optional[int]
    message: Optional[Dict[str, Any]]

async def tg_send(chat_id: int, text: str):
    if not TELEGRAM_TOKEN:
        print(f"[DRY RUN] {chat_id}: {text}")
        return
    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text},
        )

# -----------------------------------------------------------------------------
# Utility: Safety, emotion, etc.
# -----------------------------------------------------------------------------
STOP = re.compile(r"(политик|религ|насили|медицинск|вакцин|диагноз|лекарств|суицид)", re.I)
CRISIS = re.compile(r"(не хочу жить|самоповрежд|отчаяни|покончи|боль невыносима)", re.I)

def crisis_detect(t: str) -> bool: return bool(CRISIS.search(t))
def detect_emotion(t: str) -> str:
    tl = t.lower()
    if re.search(r"устал|напряж|тревож|страш|злюсь|раздраж", tl): return "tense"
    if re.search(r"спокойн|рад|легко|хорошо", tl): return "calm"
    if re.search(r"не знаю|путаюсь|сомнева", tl): return "uncertain"
    return "neutral"

# -----------------------------------------------------------------------------
# KNO (анкета)
# -----------------------------------------------------------------------------
KNO = [
    ("ei_q1", "Когда ты устаёшь — что помогает быстрее восстановиться: пообщаться с людьми 🌿 или побыть наедине ☁️?"),
    ("sn_q1", "Что тебе ближе: действовать по конкретным шагам и фактам 🔎 или ориентироваться на идею и смысл ✨?"),
    ("tf_q1", "Как ты чаще принимаешь решения: через логику и аргументы 🧠 или через чувства и внутренние ценности 💛?"),
    ("jp_q1", "Когда тебе спокойнее: когда всё чётко спланировано 📋 или когда есть свобода и импровизация 🎨?"),
    ("jp_q2", "Когда много задач: список заранее ✅ или пробовать и смотреть по ситуации 🧭?"),
    ("ei_q2", "Когда нужно разобраться: поговорить с кем-то 🗣 или записать мысли для себя ✍️?")
]
KNO_MAP = {"ei_q1":("E","I"), "sn_q1":("S","N"), "tf_q1":("T","F"),
           "jp_q1":("J","P"), "jp_q2":("J","P"), "ei_q2":("E","I")}

def ensure_user(uid:int, username=None, first_name=None, last_name=None):
    q("""INSERT INTO user_profile(user_id,username,first_name,last_name)
         VALUES(%s,%s,%s,%s)
         ON CONFLICT (user_id) DO NOTHING""",
      (uid,username,first_name,last_name), fetch=False)

def app_state_get(uid:int)->Dict[str,Any]:
    r = q("SELECT facts FROM user_profile WHERE user_id=%s",(uid,))
    if not r: return {}
    return r[0]["facts"].get("app_state",{}) if r[0]["facts"] else {}

def app_state_set(uid:int, patch:Dict[str,Any]):
    r = q("SELECT facts FROM user_profile WHERE user_id=%s",(uid,))
    facts = r[0]["facts"] if r and r[0]["facts"] else {}
    st = facts.get("app_state",{})
    st.update(patch)
    facts["app_state"] = st
    q("UPDATE user_profile SET facts=%s, updated_at=NOW() WHERE user_id=%s",
      (json.dumps(facts),uid), fetch=False)

# -----------------------------------------------------------------------------
# Semantic helpers (on-topic)
# -----------------------------------------------------------------------------
def embed(text: str) -> List[float]:
    # Dummy embedding for demo; plug in real model later
    return [float(len(text)%5)/10.0 for _ in range(32)]

def cos_sim(a: List[float], b: List[float]) -> float:
    num = sum(x*y for x,y in zip(a,b))
    den = math.sqrt(sum(x*x for x in a)) * math.sqrt(sum(y*y for y in b))
    return num/den if den else 0.0

def on_topic_score(uid:int, user_text:str)->float:
    st = app_state_get(uid)
    goal = st.get("session_goal")
    if not goal: return 1.0
    gv = st.get("goal_vec") or embed(goal)
    app_state_set(uid, {"goal_vec": gv})
    return cos_sim(gv, embed(user_text))

# -----------------------------------------------------------------------------
# Self-check system
# -----------------------------------------------------------------------------
def has_tool(text:str)->bool:
    return bool(re.search(r"(попробуй|сделай|шаг|в течение|минут|упражн|практик|план|конкретно)", text.lower()))

def has_focus_question(text:str)->bool:
    return "?" in text and bool(re.search(r"(что|как|когда|где|какой|какие)\b", text.lower()))

def self_check(uid:int, answer:str, user_text:str)->Dict[str,Any]:
    score = on_topic_score(uid, user_text)
    return {
        "on_topic": round(score,2),
        "has_tool": has_tool(answer),
        "has_emp": bool(re.search(r"(слышу|вижу|понимаю|рядом|важно)", answer.lower())),
        "has_focus_q": has_focus_question(answer),
        "length_ok": 90 <= len(answer) <= 350
    }

# -----------------------------------------------------------------------------
# Reply system (simplified)
# -----------------------------------------------------------------------------
def personalized_reply(uid:int, text:str, phase:str)->str:
    t=text.lower()
    if "стресс" in t or "устал" in t:
        return "Понимаю, как непросто бывает. Попробуй сделать короткую паузу на дыхание — 4 вдоха, 7 задержка, 8 выдох. Что помогает тебе восстановиться быстрее?"
    if "план" in t or "цель" in t:
        return "Хорошо, что думаешь о планах. Давай выберем 1 маленький шаг, который можно сделать сегодня — что это будет?"
    if "отнош" in t or "чувств" in t:
        return "Слышу, что тебе важно в отношениях. Что сейчас для тебя самое главное — поддержка, понимание или пространство?"
    return "Я рядом и слышу тебя. Что сейчас для тебя самое важное?"

# -----------------------------------------------------------------------------
# Telegram webhook
# -----------------------------------------------------------------------------
@app.post("/webhook/telegram")
async def webhook(update: TelegramUpdate, request: Request):
    try:
        if not update.message:
            return {"ok":True}
        msg = update.message
        chat_id = msg["chat"]["id"]
        uid = chat_id
        text = (msg.get("text") or "").strip()
        u = msg.get("from",{})
        ensure_user(uid, u.get("username"), u.get("first_name"), u.get("last_name"))

        st = app_state_get(uid)

        # Step 1 — after onboarding, ask for goal
        if st.get("kno_done") and not st.get("session_goal"):
            await tg_send(chat_id, "Чтобы мне было полезнее, расскажи коротко — с чем хочешь сегодня поработать или о чём поговорить?")
            app_state_set(uid, {"session_goal_pending":True})
            return {"ok":True}

        # Step 2 — save goal
        if st.get("session_goal_pending"):
            app_state_set(uid, {"session_goal": text, "session_goal_pending":False})
            await tg_send(chat_id, f"Приняла 💛 Цель записала: «{text}». Я помогу держать фокус и не распыляться.")
            return {"ok":True}

        # Safety
        if crisis_detect(text):
            await tg_send(chat_id, "Я рядом и слышу твою боль. Сейчас важно не оставаться одной/одному — обратись к близким или службе помощи 💛")
            return {"ok":True}
        if STOP.search(text):
            await tg_send(chat_id, "Давай оставим чувствительные темы. Расскажи, что тебе важнее сейчас?")
            return {"ok":True}

        # On-topic check
        score = on_topic_score(uid, text)
        if score < 0.55:
            goal = app_state_get(uid).get("session_goal","твоей теме")
            await tg_send(chat_id, f"Вижу, что ты уходишь немного в сторону. Давай сначала завершим разговор по теме «{goal}». Верно?")
            return {"ok":True}

        # Generate answer
        draft = personalized_reply(uid, text, "focus")

        # Quality check
        ql = self_check(uid, draft, text)
        if not (ql["on_topic"] >= 0.6 and ql["has_tool"] and ql["has_focus_q"]):
            draft = ("Слышу тебя. Чтобы продвинуться по твоей теме — выдели 5 минут и запиши 3 мысли, "
                     "которые помогут сделать шаг вперёд. Что из этого кажется тебе самым реалистичным?")
            ql = self_check(uid, draft, text)

        await tg_send(chat_id, draft)

        q("""INSERT INTO dialog_events(user_id,role,text,mi_phase,emotion,quality)
             VALUES(%s,'assistant',%s,%s,%s,%s)""",
          (uid, draft, "focus", detect_emotion(text), json.dumps(ql)), fetch=False)

        return {"ok":True}

    except Exception as e:
        print("Webhook error:", e)
        traceback.print_exc()
        return {"ok":False}

# -----------------------------------------------------------------------------
@app.get("/")
async def root(): return {"ok":True,"service":"anima-v4"}

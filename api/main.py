# api/main.py — ANIMA 2.0 (v5, adaptive)
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
app = FastAPI(title="ANIMA 2.0 (v5 adaptive)")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
DB_URL = os.getenv("DATABASE_URL", "")
REPORTS_TOKEN = os.getenv("REPORTS_TOKEN", "")

# -----------------------------------------------------------------------------
# DB helpers
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

# минимальные авто-миграции для устойчивости
safe_exec("""
CREATE TABLE IF NOT EXISTS user_profile (
  user_id BIGINT PRIMARY KEY,
  username TEXT,
  first_name TEXT,
  last_name TEXT,
  locale TEXT,
  facts JSONB DEFAULT '{}'::jsonb,
  created_at TIMESTAMP DEFAULT NOW(),
  updated_at TIMESTAMP DEFAULT NOW()
);
""")
safe_exec("""
CREATE TABLE IF NOT EXISTS dialog_events (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT REFERENCES user_profile(user_id) ON DELETE CASCADE,
  role TEXT CHECK (role IN ('user','assistant','system')),
  text TEXT,
  emotion TEXT,
  mi_phase TEXT,
  topic TEXT,
  relevance BOOLEAN,
  axes JSONB,
  quality JSONB,
  created_at TIMESTAMP DEFAULT NOW()
);
""")
# на случай старой схемы без колонки quality
safe_exec("ALTER TABLE dialog_events ADD COLUMN IF NOT EXISTS quality JSONB;")
safe_exec("CREATE INDEX IF NOT EXISTS idx_dialog_user_created ON dialog_events(user_id, created_at DESC);")

safe_exec("""
CREATE TABLE IF NOT EXISTS psycho_profile (
  user_id BIGINT PRIMARY KEY REFERENCES user_profile(user_id) ON DELETE CASCADE,
  ei FLOAT DEFAULT 0.5,
  sn FLOAT DEFAULT 0.5,
  tf FLOAT DEFAULT 0.5,
  jp FLOAT DEFAULT 0.5,
  confidence FLOAT DEFAULT 0.3,
  mbti_type TEXT,
  anchors JSONB DEFAULT '[]'::jsonb,
  state TEXT,
  updated_at TIMESTAMP DEFAULT NOW()
);
""")
safe_exec("CREATE UNIQUE INDEX IF NOT EXISTS ux_psycho_profile_user ON psycho_profile(user_id);")

safe_exec("""
CREATE TABLE IF NOT EXISTS daily_topics (
  user_id BIGINT PRIMARY KEY REFERENCES user_profile(user_id) ON DELETE CASCADE,
  topics JSONB NOT NULL,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
""")

# -----------------------------------------------------------------------------
# Telegram types
# -----------------------------------------------------------------------------
class TelegramUpdate(BaseModel):
    update_id: Optional[int]
    message: Optional[Dict[str, Any]]

async def tg_send(chat_id: int, text: str):
    if not TELEGRAM_TOKEN:
        print(f"[DRY RUN] {chat_id}: {text}")
        return
    async with httpx.AsyncClient(timeout=20) as client:
        await client.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text},
        )

# -----------------------------------------------------------------------------
# Safety, emotion
# -----------------------------------------------------------------------------
STOP = re.compile(r"(политик|религ|насили|медицинск|вакцин|диагноз|лекарств|суицид)", re.I)
CRISIS = re.compile(r"(не хочу жить|самоповрежд|отчаяни|покончи|боль невыносима)", re.I)

def crisis_detect(t: str) -> bool: return bool(CRISIS.search(t or ""))

def detect_emotion(t: str) -> str:
    tl = (t or "").lower()
    if re.search(r"устал|напряж|тревож|страш|злюсь|злость|раздраж", tl): return "tense"
    if re.search(r"спокойн|рад|легко|хорошо", tl): return "calm"
    if re.search(r"не знаю|путаюсь|сомнева", tl): return "uncertain"
    return "neutral"

# -----------------------------------------------------------------------------
# User state helpers
# -----------------------------------------------------------------------------
def ensure_user(uid:int, username=None, first_name=None, last_name=None):
    q("""INSERT INTO user_profile(user_id,username,first_name,last_name)
         VALUES(%s,%s,%s,%s)
         ON CONFLICT (user_id) DO NOTHING""",
      (uid,username,first_name,last_name), fetch=False)

def get_facts(uid:int)->Dict[str,Any]:
    r = q("SELECT facts FROM user_profile WHERE user_id=%s",(uid,))
    return (r[0]["facts"] if r and r[0]["facts"] else {})

def set_facts(uid:int, facts:Dict[str,Any]):
    q("UPDATE user_profile SET facts=%s, updated_at=NOW() WHERE user_id=%s",
      (json.dumps(facts),uid), fetch=False)

def app_state_get(uid:int)->Dict[str,Any]:
    facts = get_facts(uid)
    return facts.get("app_state",{})

def app_state_set(uid:int, patch:Dict[str,Any]):
    facts = get_facts(uid)
    st = facts.get("app_state",{})
    st.update(patch)
    facts["app_state"] = st
    set_facts(uid, facts)

# -----------------------------------------------------------------------------
# Semantic on-topic (placeholder embeddings)
# -----------------------------------------------------------------------------
def embed(text: str) -> List[float]:
    # Placeholder без внешних API: стабильный, но «грубый» сигнал
    return [float((sum(ord(ch) for ch in text) % 97))/100.0 for _ in range(32)] if text else [0.0]*32

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
    return cos_sim(gv, embed(user_text or ""))

# -----------------------------------------------------------------------------
# Adaptive style profile (self-learning)
# -----------------------------------------------------------------------------
# мы ведём сглаженные метрики стиля в facts.style_profile
DEFAULT_STYLE = {
    "formality": 0.5,     # 0 простая речь, 1 очень формальная
    "emoji_rate": 0.2,    # доля сообщений с эмодзи
    "brevity": 0.5,       # 0 любит разворачивать, 1 лаконичен
    "asks_for_steps": 0.5,# склонность просить конкретные шаги
    "asks_for_meaning":0.5,# склонность говорить о смысле/ценностях
    "pace": 0.5,          # темп диалога: 0 медленный, 1 быстрый
    "pref_greet": 1.0     # любит тёплое приветствие в начале
}

EMOJI_RE = re.compile(
    "["u"\U0001F300-\U0001FAD6"
    u"\U0001F600-\U0001F64F"
    u"\U0001F680-\U0001F6FF"
    u"\U0001F300-\U0001F5FF"
    u"\U0001F1E0-\U0001F1FF"
    "]+", flags=re.UNICODE
)

def ewma(current: float, new_value: float, alpha: float = 0.12) -> float:
    return max(0.0, min(1.0, current*(1-alpha) + new_value*alpha))

def analyze_user_style(text: str) -> Dict[str,float]:
    tl = (text or "").lower()
    # простые эвристики
    has_emoji = bool(EMOJI_RE.search(text or ""))
    formality = 0.7 if re.search(r"(пожалуйста|необходимо|полагаю|соответственно|уточните)", tl) else 0.3
    brevity = 0.7 if len(tl) < 100 else 0.3
    asks_steps = 0.7 if re.search(r"(конкретн|по шагам|пошаг|что делать|как именно)", tl) else 0.3
    asks_meaning = 0.7 if re.search(r"(смысл|ценност|зачем|для чего)", tl) else 0.3
    pace = 0.7 if re.search(r"(быстрее|срочно|давай сразу|коротко)", tl) else 0.3
    return {
        "formality": formality,
        "emoji_rate": 0.8 if has_emoji else 0.1,
        "brevity": brevity,
        "asks_for_steps": asks_steps,
        "asks_for_meaning": asks_meaning,
        "pace": pace
    }

def update_style_profile(uid:int, features:Dict[str,float]):
    facts = get_facts(uid)
    style = facts.get("style_profile", DEFAULT_STYLE.copy())
    for k,v in features.items():
        style[k] = ewma(style.get(k, DEFAULT_STYLE.get(k,0.5)), v)
    facts["style_profile"] = style
    set_facts(uid, facts)

def style_for_reply(uid:int)->Dict[str,Any]:
    facts = get_facts(uid)
    style = facts.get("style_profile", DEFAULT_STYLE.copy())
    # производные настройки ответа
    length_target = 140 if style["brevity"] >= 0.6 else 240
    use_emoji = style["emoji_rate"] >= 0.4
    tone = "аккуратно и кратко" if style["formality"] >= 0.6 else "тепло и простым языком"
    steps_bias = style["asks_for_steps"] >= style["asks_for_meaning"]
    return {
        "tone": tone,
        "length_target": length_target,
        "use_emoji": use_emoji,
        "prefer_steps": steps_bias
    }

# -----------------------------------------------------------------------------
# Self-check for quality
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
# Reply planner + adaptive reply
# -----------------------------------------------------------------------------
def plan_reply(goal:str, prefer_steps:bool)->List[str]:
    if prefer_steps:
        return [
            f"Коротко отзеркаль цель: {goal}",
            "Дай 1–2 конкретных шага/инструмента",
            "Предложи мини-действие на сегодня",
            "Заверши фокус-вопросом"
        ]
    else:
        return [
            f"Коротко отзеркаль цель: {goal}",
            "Сформулируй смысл/рамку, почему это важно",
            "Предложи мягкий эксперимент",
            "Заверши фокус-вопросом"
        ]

def build_adaptive_reply(uid:int, user_text:str, phase:str) -> str:
    st = app_state_get(uid)
    goal = st.get("session_goal","твою тему")
    style = style_for_reply(uid)
    prefer_steps = style["prefer_steps"]
    plan = plan_reply(goal, prefer_steps)

    # базовые готовые микро-инструменты
    tools_steps = "Выдели 5 минут, запиши 3 коротких шага и начни с самого лёгкого."
    tools_meaning = "Сформулируй, ради чего это тебе важно, в одном предложении. Это снизит расфокус."

    # эмоциональное отражение
    tl = user_text.lower()
    if re.search(r"устал|напряж|тревож|злюсь|раздраж", tl):
        empath = "Слышу напряжение — бережно отнесёмся к твоему ресурсу. "
    elif re.search(r"спокойн|легко|получилось|рад", tl):
        empath = "Чувствую спокойствие и готовность двигаться. "
    else:
        empath = "Я рядом и внимательно слушаю. "

    # содержательная часть
    if prefer_steps:
        body = f"Чтобы продвинуться, {tools_steps}"
        focus_q = "С чего начнём прямо сегодня?"
    else:
        body = f"Чтобы не потерять смысл, {tools_meaning}"
        focus_q = "Какой образ или мысль сейчас больше откликается?"

    # стиль оформления
    postfix = " Ты не одна и не один, я здесь." if style["tone"].startswith("тепло") else ""
    emoji = " ✨" if style["use_emoji"] else ""

    draft = f"{empath}По твоей цели «{goal}» я предлагаю так: {body} {focus_q}{emoji}{postfix}"

    # подстройка длины простым способом
    if style["length_target"] < 180 and len(draft) > 220:
        # сжать фразу
        draft = re.sub(r"\s{2,}", " ", draft)
        draft = draft.replace("Я рядом и внимательно слушаю. ", "")
        draft = draft.replace("Чтобы не потерять смысл, ", "")
        draft = draft.replace("Чтобы продвинуться, ", "")

    return draft

# -----------------------------------------------------------------------------
# Feedback: thumbs up/down адаптация
# -----------------------------------------------------------------------------
POS_FEEDBACK = re.compile(r"(👍|спасибо|полезно|супер|отлично|помогло)", re.I)
NEG_FEEDBACK = re.compile(r"(👎|не очень|не помогло|плохо|мимо)", re.I)

def apply_feedback(uid:int, text:str):
    if POS_FEEDBACK.search(text or ""):
        # усилим текущие предпочтения: чуть больше краткости и шагов
        f = get_facts(uid)
        style = f.get("style_profile", DEFAULT_STYLE.copy())
        style["brevity"] = ewma(style.get("brevity",0.5), 0.7)
        style["asks_for_steps"] = ewma(style.get("asks_for_steps",0.5), 0.7)
        f["style_profile"] = style
        set_facts(uid, f)
    elif NEG_FEEDBACK.search(text or ""):
        # ослабим шаги, добавим смысла и тепла
        f = get_facts(uid)
        style = f.get("style_profile", DEFAULT_STYLE.copy())
        style["asks_for_steps"] = ewma(style.get("asks_for_steps",0.5), 0.3)
        style["formality"] = ewma(style.get("formality",0.5), 0.4)
        style["emoji_rate"] = ewma(style.get("emoji_rate",0.2), 0.5)
        f["style_profile"] = style
        set_facts(uid, f)

# -----------------------------------------------------------------------------
# Webhook
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

        # быстрый фидбек
        apply_feedback(uid, text)

        # онбординг / старт цели если анкета уже была где-то выше твоей логики
        st = app_state_get(uid)
        if st.get("kno_done") and not st.get("session_goal"):
            await tg_send(chat_id, "Чтобы мне быть полезнее, расскажи коротко — с чем хочешь сегодня поработать?")
            app_state_set(uid, {"session_goal_pending":True})
            return {"ok":True}
        if st.get("session_goal_pending"):
            app_state_set(uid, {"session_goal": text, "session_goal_pending":False, "goal_vec": embed(text)})
            await tg_send(chat_id, f"Приняла 💛 Цель записала: «{text}». Я помогу держать фокус.")
            return {"ok":True}

        # Safety
        if crisis_detect(text):
            await tg_send(chat_id, "Я рядом и слышу твою боль. Важно не оставаться одной или одному — обратись к близким или в службу помощи вашего города 💛")
            return {"ok":True}
        if STOP.search(text):
            await tg_send(chat_id, "Давай оставим чувствительные темы. Расскажи, что тебе важнее сейчас?")
            return {"ok":True}

        # Самообучение по стилю пользователя на каждом сообщении
        update_style_profile(uid, analyze_user_style(text))

        # Удержание темы
        score = on_topic_score(uid, text)
        if score < 0.55:
            goal = app_state_get(uid).get("session_goal","текущей теме")
            await tg_send(chat_id, f"Вижу, что мы уходим в сторону. Давай сначала продвинемся по теме «{goal}». Если хочешь сменить фокус — скажи, и я переключусь.")
            return {"ok":True}

        # Генерация адаптивного ответа
        draft = build_adaptive_reply(uid, text, "focus")

        # Self-check качества
        quality = self_check(uid, draft, text)
        if not (quality["on_topic"] >= 0.6 and quality["has_tool"] and quality["has_focus_q"] and quality["length_ok"]):
            # компактная ремонтная версия с акцентом на шаг
            draft = ("Слышу тебя. Чтобы продвинуться по твоей теме — выдели 5 минут и запиши 3 коротких шага. "
                     "Выбери один самый лёгкий и сделай его сегодня. Что возьмёшь первым?")
            quality = self_check(uid, draft, text)

        await tg_send(chat_id, draft)

        # Лог
        q("""INSERT INTO dialog_events(user_id,role,text,mi_phase,emotion,quality)
             VALUES(%s,'assistant',%s,%s,%s,%s)""",
          (uid, draft, "focus", detect_emotion(text), json.dumps(quality)), fetch=False)

        return {"ok":True}

    except Exception as e:
        print("Webhook error:", e)
        traceback.print_exc()
        try:
            if update and update.message:
                chat_id = update.message["chat"]["id"]
                await tg_send(chat_id, "Кажется, я споткнулась о техническую мелочь. Уже поправляю — можно повторить последнюю мысль?")
        except Exception:
            pass
        return {"ok":False}

# -----------------------------------------------------------------------------
# Reports (как было)
# -----------------------------------------------------------------------------
def auth_reports(x_token: str) -> bool:
    return (not REPORTS_TOKEN) or (x_token == REPORTS_TOKEN)

@app.get("/reports/summary")
async def reports_summary(x_token: str = Header(default="")):
    if not auth_reports(x_token):
        return {"error":"unauthorized"}
    kpi = q("""
      WITH ql AS (
        SELECT avg_quality, safety_rate, answers_total
        FROM v_quality_score
        ORDER BY day DESC LIMIT 30
      ),
      ph AS (
        SELECT mi_phase, sum(cnt) AS cnt
        FROM v_phase_dist
        WHERE day >= NOW() - INTERVAL '30 days'
        GROUP BY mi_phase
      )
      SELECT
        (SELECT avg(avg_quality) FROM ql) AS avg_quality_30d,
        (SELECT avg(safety_rate) FROM ql) AS safety_rate_30d,
        (SELECT sum(answers_total) FROM ql) AS answers_30d,
        (SELECT json_agg(json_build_object('phase', mi_phase, 'count', cnt)) FROM ph) AS phases
    """)
    conf = q("SELECT * FROM v_confidence_hist")
    ret  = q("SELECT * FROM v_retention_7d")
    return {
        "kpi": kpi[0] if kpi else {},
        "confidence_hist": conf or [],
        "retention7d": ret[0] if ret else {}
    }

@app.get("/")
async def root(): return {"ok":True,"service":"anima-v5-adaptive"}

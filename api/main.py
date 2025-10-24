import os, re, json, math, hashlib, traceback, random
from typing import Any, Dict, Optional, List, Tuple
from fastapi import FastAPI, Request, Header
from pydantic import BaseModel
from dotenv import load_dotenv
import httpx
import psycopg2, psycopg2.extras
from datetime import datetime

load_dotenv()
app = FastAPI(title="ANIMA 2.0")

# --- ENV ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
DB_URL         = os.getenv("DATABASE_URL", "")
REPORTS_TOKEN  = os.getenv("REPORTS_TOKEN", "")

# =========================================
#                DB LAYER
# =========================================
def db():
    return psycopg2.connect(DB_URL)

def q(query: str, params: Tuple = (), fetch: bool = True):
    """
    Простая обёртка над psycopg2.
    fetch=True  -> вернуть rows (RealDict)
    fetch=False -> просто выполнить (INSERT/UPDATE/DELETE)
    """
    conn = db()
    try:
        with conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(query, params)
                if fetch and cur.description:
                    return cur.fetchall()
                return None
    finally:
        conn.close()

def ensure_user(uid:int, username=None, first_name=None, last_name=None):
    q("""INSERT INTO user_profile(user_id,username,first_name,last_name)
         VALUES(%s,%s,%s,%s)
         ON CONFLICT (user_id) DO NOTHING""",
      (uid,username,first_name,last_name), fetch=False)

def app_state_get(uid:int)->Dict[str,Any]:
    r = q("SELECT facts FROM user_profile WHERE user_id=%s",(uid,))
    if not r:
        return {}
    return r[0]["facts"].get("app_state",{}) if r[0]["facts"] else {}

def app_state_patch(uid:int, patch:Dict[str,Any]):
    r = q("SELECT facts FROM user_profile WHERE user_id=%s",(uid,))
    facts = r[0]["facts"] if r and r[0]["facts"] else {}
    st = facts.get("app_state",{})
    st.update(patch)
    facts["app_state"] = st
    q("UPDATE user_profile SET facts=%s, updated_at=NOW() WHERE user_id=%s",
      (json.dumps(facts),uid), fetch=False)

# =========================================
#            TELEGRAM I/O
# =========================================
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

# =========================================
#          SAFETY / HEURISTICS
# =========================================
STOP   = re.compile(r"(политик|религ|насили|медицинск|вакцин|диагноз|лекарств|суицид)", re.IGNORECASE)
CRISIS = re.compile(r"(не хочу жить|самоповрежд|отчаяни|суицид|покончи|боль невыносима)", re.IGNORECASE)

def crisis_detect(t: str) -> bool:
    return bool(CRISIS.search(t or ""))

def detect_emotion(t: str) -> str:
    tl = (t or "").lower()
    if re.search(r"устал|напряж|тревож|страш|злюсь|злость|раздраж", tl): return "tense"
    if re.search(r"спокойн|рад|легко|хорошо", tl):                   return "calm"
    if re.search(r"не знаю|путаюсь|сомнева", tl):                    return "uncertain"
    return "neutral"

# =========================================
#           TINY EMBEDDINGS (local)
# =========================================
DIM = 16
def _hash_token(tok: str) -> int:
    return int(hashlib.sha256(tok.encode("utf-8")).hexdigest()[:8], 16)

def embed(text: str) -> List[float]:
    vec = [0.0]*DIM
    if not text:
        return vec
    for tok in re.findall(r"\w+", text.lower()):
        h = _hash_token(tok)
        vec[h % DIM] += 1.0
    # l2 normalize
    norm = math.sqrt(sum(v*v for v in vec)) or 1.0
    return [v/norm for v in vec]

def cos(a: List[float], b: List[float]) -> float:
    return sum(x*y for x,y in zip(a,b))

# =========================================
#          STYLE / PROFILE (simple)
# =========================================
def ensure_profile(uid:int):
    r = q("SELECT user_id FROM psycho_profile WHERE user_id=%s",(uid,))
    if not r:
        q("INSERT INTO psycho_profile(user_id) VALUES(%s)",(uid,), fetch=False)

def update_style_profile(uid:int, signals: Dict[str,float]):
    """
    Простое EWMA обновление признаков профиля (ei,sn,tf,jp) по эвристикам.
    """
    ensure_profile(uid)
    row = q("SELECT ei,sn,tf,jp,confidence,anchors FROM psycho_profile WHERE user_id=%s",(uid,))
    if not row: return
    p = row[0]
    ei,sn,tf,jp = (p["ei"] or 0.5),(p["sn"] or 0.5),(p["tf"] or 0.5),(p["jp"] or 0.5)

    def ewma(val, delta, a=0.10):  # мягко
        return max(0.0, min(1.0, val + a*delta))

    if "ei" in signals: ei = ewma(ei, signals["ei"])
    if "sn" in signals: sn = ewma(sn, signals["sn"])
    if "tf" in signals: tf = ewma(tf, signals["tf"])
    if "jp" in signals: jp = ewma(jp, signals["jp"])

    conf = min(0.99, (p["confidence"] or 0.3) + 0.02)
    anchors = (p["anchors"] or [])[:48]
    if signals.get("_anchor"):
        anchors.append(signals["_anchor"])

    q("""UPDATE psycho_profile SET ei=%s,sn=%s,tf=%s,jp=%s,confidence=%s,anchors=%s,updated_at=NOW()
         WHERE user_id=%s""",
      (ei,sn,tf,jp,conf,json.dumps(anchors),uid), fetch=False)

def analyze_user_style(text:str)->Dict[str,float]:
    """Эвристики для стиля пользователя по реплике."""
    tl = (text or "").lower()
    sig: Dict[str,float] = {}
    if re.search(r"вместе|обсудим|люд|команд", tl): sig["ei"]=+0.2; sig["_anchor"]={"axis":"ei","quote":"про людей"}
    if re.search(r"один|наедине|тишин", tl):         sig["ei"]=-0.2; sig["_anchor"]={"axis":"ei","quote":"уединение"}
    if re.search(r"факт|шаг|конкрет", tl):           sig["sn"]=-0.15; sig["_anchor"]={"axis":"sn","quote":"факты"}
    if re.search(r"смысл|идея|картина", tl):         sig["sn"]=+0.15; sig["_anchor"]={"axis":"sn","quote":"смысл"}
    if re.search(r"логик|аргумент|сравн", tl):       sig["tf"]=+0.15; sig["_anchor"]={"axis":"tf","quote":"анализ"}
    if re.search(r"чувств|ценност|эмоци", tl):       sig["tf"]=-0.15; sig["_anchor"]={"axis":"tf","quote":"эмпатия"}
    if re.search(r"план|распис|контрол", tl):        sig["jp"]=+0.2;  sig["_anchor"]={"axis":"jp","quote":"планирование"}
    if re.search(r"свобод|импров|спонтан", tl):      sig["jp"]=-0.2;  sig["_anchor"]={"axis":"jp","quote":"гибкость"}
    return sig

def comms_style(uid:int)->Dict[str,str]:
    ensure_profile(uid)
    p = q("SELECT ei,sn,tf,jp FROM psycho_profile WHERE user_id=%s",(uid,))[0]
    return {
        "tone":   "активный" if (p["ei"] or 0.5) >= 0.5 else "спокойный",
        "detail": "смыслы"   if (p["sn"] or 0.5) >= 0.5 else "шаги",
        "mind":   "анализ"   if (p["tf"] or 0.5) >= 0.5 else "чувства",
        "plan":   "план"     if (p["jp"] or 0.5) >= 0.5 else "эксперимент"
    }

# =========================================
#          GOAL / FOCUS CONTROL
# =========================================
def on_topic_score(uid:int, user_text:str) -> float:
    st = app_state_get(uid)
    goal_vec = st.get("goal_vec")
    if not goal_vec:
        return 1.0
    v1 = goal_vec
    v2 = embed(user_text or "")
    return max(0.0, min(1.0, cos(v1, v2)))

def reflect_emotion(text:str)->str:
    t=(text or "").lower()
    if re.search(r"устал|напряж|тревож|злюсь|злость|раздраж",t): return "Слышу напряжение и заботу о результате. "
    if re.search(r"спокойн|рад|легко|получилось",t):            return "Чувствую спокойствие и лёгкость. "
    if re.search(r"не знаю|путаюсь|сомнева",t):                  return "Вижу, что хочется ясности. "
    return "Я рядом и слышу тебя. "

def build_adaptive_reply(uid:int, user_text:str, phase:str="focus")->str:
    st = comms_style(uid)
    emo_hint = reflect_emotion(user_text)
    if phase == "focus":
        q1 = "На чём тебе хочется остановиться в первую очередь?"
    else:
        q1 = "Что здесь для тебя главное?"

    # Лёгкий инструмент/шаг
    tool = ""
    if st["detail"] == "шаги":
        tool = " Давай выберем один маленький шаг на сегодня (5–10 минут). Какой подойдёт лучше всего?"
    else:
        tool = " Какой смысл видишь в ситуации и что это говорит о твоих ценностях?"

    return f"{emo_hint}{q1}{tool}"

# =========================================
#         SELF CHECK (на своём борте)
# =========================================
def self_check(uid:int, draft:str, user_text:str)->Dict[str,Any]:
    st = app_state_get(uid)
    goal = st.get("session_goal","")
    score = on_topic_score(uid, draft + " " + user_text)
    return {
        "on_topic": score,
        "has_question": ("?" in draft),
        "has_tool": bool(re.search(r"(шаг|попробу|упражн|план|эксперимент)", draft.lower())),
        "tone": "supportive"
    }

# =========================================
#         FEEDBACK SHORTCUTS
# =========================================
def apply_feedback(uid:int, text:str):
    tl = (text or "").lower()
    if "слишком длин" in tl:
        app_state_patch(uid, {"pref_short": True})
    if "больше конкретики" in tl or "конкретн" in tl:
        app_state_patch(uid, {"pref_concrete": True})

# =========================================
#         ONBOARDING (first meet)
# =========================================
ONB_STEPS = [
    {
        "key":"greet",
        "ask":(
            "Привет 🌿 Я Анима — твой личный психологический ассистент. "
            "Я помогаю навести ясность, снизить стресс и наметить шаги вперед. "
            "Наши разговоры конфиденциальны, никакого спама — только поддержка 💛\n\n"
            "Как мне к тебе обращаться?"
        )
    },
    {
        "key":"mood",
        "ask":"Как ты сейчас? Выбери слово, которое ближе: спокойно, напряжённо, растерянно — или опиши по-своему."
    },
    {
        "key":"expect",
        "ask":"Чего бы тебе хотелось от наших разговоров? Больше ясности, поддержки, энергии на действия — что откликается?"
    },
    {
        "key":"goal",
        "ask":"Чтобы мне быть полезнее, расскажи кратко — с чем хочешь сегодня поработать или о чём поговорить?"
    }
]

def onboarding_start(uid:int):
    app_state_patch(uid, {
        "onboarding_idx": 0,
        "onboarding": {},
        "onboarding_pending": True,
        "session_goal": None,
        "session_goal_pending": False
    })

def onboarding_next(uid:int, text:str)->Optional[str]:
    st  = app_state_get(uid)
    idx = st.get("onboarding_idx", 0)
    data = st.get("onboarding", {})

    # сохраняем ответ на предыдущий вопрос (кроме самого первого)
    if idx > 0:
        prev_key = ONB_STEPS[idx-1]["key"]
        data[prev_key] = text

    # если отработали все — сохраняем goal, выходим
    if idx >= len(ONB_STEPS):
        # safety: если цели нет — используем mood/expect
        goal = data.get("goal") or data.get("expect") or "поддерживающий диалог"
        app_state_patch(uid, {
            "onboarding_idx": None,
            "onboarding": data,
            "onboarding_pending": False,
            "session_goal": goal,
            "goal_vec": embed(goal),
        })
        return None

    # задаём следующий
    ask = ONB_STEPS[idx]["ask"]
    app_state_patch(uid, {
        "onboarding_idx": idx+1,
        "onboarding": data,
        "onboarding_pending": True
    })
    return ask

# =========================================
#               ROUTES
# =========================================
@app.get("/")
async def root():
    return {"ok":True,"service":"anima","time":datetime.utcnow().isoformat()}

@app.post("/webhook/telegram")
async def webhook(update: TelegramUpdate, request: Request):
    try:
        if not update.message:
            return {"ok": True}

        msg = update.message
        chat_id = msg["chat"]["id"]
        uid = chat_id
        text_raw = (msg.get("text") or "").strip()
        text = text_raw.lower()
        u = msg.get("from", {})
        ensure_user(uid, u.get("username"), u.get("first_name"), u.get("last_name"))

        # /start -> всегда запускаем мягкий онбординг
        if text in ("/start","start","привет","начать"):
            onboarding_start(uid)
            await tg_send(chat_id, ONB_STEPS[0]["ask"])
            return {"ok": True}

        # --- Onboarding flow ---
        st = app_state_get(uid)
        if st.get("onboarding_pending"):
            nxt = onboarding_next(uid, text_raw)
            if nxt is None:
                final = (
                    "Спасибо, я записала 💛 Если захочешь изменить фокус — просто напиши.\n\n"
                    "Готова продолжать. Скажи, пожалуйста, какой маленький шаг по этой теме был бы для тебя посильным сегодня?"
                )
                await tg_send(chat_id, final)
            else:
                await tg_send(chat_id, nxt)
            return {"ok": True}

        # --- Goal capture (если кто-то очистил состояние) ---
        if st.get("session_goal_pending"):
            app_state_patch(uid, {
                "session_goal": text_raw,
                "session_goal_pending": False,
                "goal_vec": embed(text_raw)
            })
            await tg_send(chat_id, f"Отлично 🌱 Я записала твою цель: «{text_raw}». Поехали дальше.")
            return {"ok": True}

        # --- Safety gates ---
        if crisis_detect(text_raw):
            await tg_send(chat_id,
                "Я рядом и слышу твою боль 💛 Если нужна срочная поддержка — обратись к близким или на горячую линию.\n"
                "Сейчас не оставайся одна. Что было бы самым бережным шагом прямо сейчас?")
            return {"ok": True}

        if STOP.search(text_raw):
            await tg_send(chat_id, "Давай оставим чувствительные темы за рамками. О чём тебе важнее поговорить сейчас?")
            return {"ok": True}

        # --- Feedback shortcuts + profile updates ---
        apply_feedback(uid, text_raw)
        update_style_profile(uid, analyze_user_style(text_raw))

        # --- Keep focus on the session goal ---
        score = on_topic_score(uid, text_raw)
        if score < 0.55:
            goal = app_state_get(uid).get("session_goal", "текущей теме")
            await tg_send(chat_id, f"Кажется, мы чуть ушли в сторону 🌿 Давай сначала завершим разговор о «{goal}». Если захочешь сменить тему — скажи \"сменим тему на ...\".")
            return {"ok": True}

        # --- Compose reply ---
        reply = build_adaptive_reply(uid, text_raw, "focus")
        qc = self_check(uid, reply, text_raw)

        # Минимальная гарантия качества
        if not (qc["on_topic"] >= 0.6 and qc["has_question"] and qc["has_tool"]):
            reply = (
                "Слышу тебя 💛 Чтобы продвинуться по твоей теме — выдели 5–10 минут и выпиши 3 мысли/шага. "
                "Какой из них попробуешь сегодня? Я помогу уточнить."
            )
            qc = self_check(uid, reply, text_raw)

        await tg_send(chat_id, reply)

        # --- Log both sides (user + assistant) ---
        q("""INSERT INTO dialog_events(user_id,role,text,emotion)
             VALUES(%s,'user',%s,%s)""",
          (uid, text_raw, detect_emotion(text_raw)), fetch=False)

        q("""INSERT INTO dialog_events(user_id,role,text,mi_phase,emotion,axes)
             VALUES(%s,'assistant',%s,%s,%s,%s)""",
          (uid, reply, "focus", detect_emotion(text_raw), json.dumps(qc)), fetch=False)

        return {"ok": True}

    except Exception as e:
        print("Webhook error:", e)
        traceback.print_exc()
        try:
            if update and update.message:
                chat_id = update.message["chat"]["id"]
                await tg_send(chat_id, "Кажется, я споткнулась о техническую мелочь 😅 Повтори, пожалуйста, последний вопрос.")
        except Exception:
            pass
        return {"ok": False}

# =========================================
#              REPORTS (same)
# =========================================
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

@app.get("/reports/user/{uid}")
async def reports_user(uid: int, x_token: str = Header(default="")):
    if not auth_reports(x_token):
        return {"error":"unauthorized"}
    prof = q("SELECT * FROM psycho_profile WHERE user_id=%s",(uid,))
    last_events = q("""
      SELECT role, text, emotion, mi_phase, relevance, created_at
      FROM dialog_events
      WHERE user_id=%s
      ORDER BY id DESC LIMIT 30
    """,(uid,))
    quality = q("""
      SELECT day, avg_quality, safety_rate, answers_total
      FROM v_quality_score
      WHERE user_id=%s
      ORDER BY day DESC LIMIT 14
    """,(uid,))
    return {
        "profile": prof[0] if prof else {},
        "last_events": last_events or [],
        "quality_14d": quality or []
    }

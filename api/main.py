# api/main.py
import os, re, json, time
from typing import Any, Dict, Optional, List, Tuple
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

# одноразово создадим тех.таблицу для идемпотентности апдейтов (не упадёт, если уже есть)
q("""
CREATE TABLE IF NOT EXISTS processed_updates (
  update_id BIGINT PRIMARY KEY,
  processed_at TIMESTAMPTZ DEFAULT NOW()
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

# -------------------- Safety --------------------
STOP   = re.compile(r"(политик|религ|насили|медицинск|вакцин|диагноз|лекарств|суицид)", re.IGNORECASE)
CRISIS = re.compile(r"(не хочу жить|самоповрежд|отчаяни|суицид|покончи|боль невыносима)", re.IGNORECASE)

def crisis_detect(t: str) -> bool:
    return bool(CRISIS.search(t or ""))

# -------------------- Emotion --------------------
def detect_emotion(t: str) -> str:
    tl = (t or "").lower()
    if re.search(r"устал|напряж|тревож|страш|злюсь|злость|раздраж", tl): return "tense"
    if re.search(r"спокойн|рад|легко|хорошо", tl): return "calm"
    if re.search(r"не знаю|путаюсь|сомнева", tl): return "uncertain"
    return "neutral"

# -------------------- Phases --------------------
def choose_phase(last_phase: str, emotion: str, text: str) -> str:
    tl = (text or "").lower()
    if emotion in ("tense", "uncertain"):
        return "engage"
    if re.search(r"\bфокус\b|главн|сосредоточ", tl): return "focus"
    if re.search(r"\bпочему\b|\bзачем\b|думаю|хочу понять|кажется", tl): return "evoke"
    if re.search(r"готов|сделаю|попробую|начну|планир", tl): return "plan"
    return "focus" if last_phase == "engage" else last_phase

# -------------------- KNO (мини-анкета) --------------------
KNO: List[Tuple[str, str]] = [
    ("ei_q1", "Когда ты устаёшь — что помогает быстрее восстановиться: пообщаться с людьми 🌿 или побыть наедине ☁️?"),
    ("sn_q1", "Что тебе ближе: действовать по конкретным шагам и фактам 🎯 или ориентироваться на идею и смысл ✨?"),
    ("tf_q1", "Как ты чаще принимаешь решения: через логику и аргументы 🧠 или через чувства и внутренние ценности 💛?"),
    ("jp_q1", "Когда тебе спокойнее: когда всё чётко спланировано 📋 или когда есть свобода и импровизация 🎲?"),
    ("jp_q2", "Когда много задач: составить список заранее или пробовать и смотреть по ситуации?"),
    ("ei_q2", "Когда нужно разобраться: поговорить с кем-то или записать мысли для себя?"),
]
KNO_MAP = {"ei_q1":("E","I"), "sn_q1":("S","N"), "tf_q1":("T","F"), "jp_q1":("J","P"), "jp_q2":("J","P"), "ei_q2":("E","I")}

INTRO_TEXT = (
    "Привет 🌿 Я Анима — твой личный психологический ассистент. "
    "Я помогаю навести ясность, снизить стресс и наметить шаги вперёд. "
    "Наши разговоры конфиденциальны, никакого спама — только поддержка 💛\n\n"
    "Чтобы мне быть полезнее, мы начнём с короткой анкеты (6 вопросов). "
    "Отвечай цифрой 1 или 2, можно своими словами."
)
SUFFIX = "\n\nОтветь 1 или 2, можно словами."

def ensure_user(uid:int, username=None, first_name=None, last_name=None):
    q("""INSERT INTO user_profile(user_id,username,first_name,last_name)
         VALUES(%s,%s,%s,%s)
         ON CONFLICT (user_id) DO NOTHING""",
      (uid,username,first_name,last_name))

def app_state_get(uid:int)->Dict[str,Any]:
    r = q("SELECT facts FROM user_profile WHERE user_id=%s",(uid,))
    if not r: return {}
    facts = r[0]["facts"] or {}
    return facts.get("app_state", {}) if isinstance(facts, dict) else {}

def app_state_set(uid:int, patch:Dict[str,Any]):
    r = q("SELECT facts FROM user_profile WHERE user_id=%s",(uid,))
    facts = r[0]["facts"] if r and r[0]["facts"] else {}
    if not isinstance(facts, dict):
        facts = {}
    st = facts.get("app_state", {})
    if not isinstance(st, dict):
        st = {}
    st.update(patch)
    facts["app_state"] = st
    q("UPDATE user_profile SET facts=%s, updated_at=NOW() WHERE user_id=%s",(json.dumps(facts),uid))

def kno_start(uid:int):
    app_state_set(uid, {"kno_idx":0, "kno_answers":{}, "last_sent_at": time.time()})

def _normalize_choice(question_key: str, text: str) -> int:
    t = (text or "").strip().lower()
    if t in {"1","первый","первое","первая"}:
        return 1
    if t in {"2","второй","второе","вторая"}:
        return 2

    # мягкие эвристики
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

def kno_step(uid:int, text:str)->Optional[str]:
    st = app_state_get(uid)
    idx = st.get("kno_idx", 0)
    # защита индекса
    if not isinstance(idx, int) or idx < 0 or idx >= len(KNO):
        kno_start(uid)
        idx = 0

    answers = st.get("kno_answers", {})
    if not isinstance(answers, dict):
        answers = {}

    key, _ = KNO[idx]
    choice = _normalize_choice(key, text)
    answers[key] = choice

    idx += 1
    if idx >= len(KNO):
        # агрегируем оси
        axes = {"E":0,"I":0,"S":0,"N":0,"T":0,"F":0,"J":0,"P":0}
        for k,v in answers.items():
            a,b = KNO_MAP[k]
            axes[a if v==1 else b]+=1
        def norm(a,b): s=a+b; return ((a/(s or 1)), (b/(s or 1)))
        E,I = norm(axes["E"],axes["I"]); S,N = norm(axes["S"],axes["N"])
        T,F = norm(axes["T"],axes["F"]); J,P = norm(axes["J"],axes["P"])

        # сохраняем как черновой профиль
        q("""INSERT INTO psycho_profile(user_id,ei,sn,tf,jp,confidence,mbti_type,anchors,state)
             VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
             ON CONFLICT (user_id) DO UPDATE
             SET ei=EXCLUDED.ei,sn=EXCLUDED.sn,tf=EXCLUDED.tf,jp=EXCLUDED.jp,
                 confidence=EXCLUDED.confidence, updated_at=NOW()""",
          (uid,E,N,T,J,0.4,None,json.dumps([]),None))
        app_state_set(uid, {"kno_done":True,"kno_idx":None,"kno_answers":answers})
        return None
    else:
        # записываем следующий индекс и возвращаем след. вопрос
        app_state_set(uid, {"kno_idx":idx, "kno_answers":answers, "last_sent_at": time.time()})
        return KNO[idx][1]

# -------------------- Personalization (краткая) --------------------
def comms_style(p:Dict[str,Any])->Dict[str,str]:
    return {
        "tone":   "активный" if p.get("ei",0.5)>=0.5 else "спокойный",
        "detail": "смыслы"   if p.get("sn",0.5)>=0.5 else "шаги",
        "mind":   "анализ"   if p.get("tf",0.5)>=0.5 else "чувства",
        "plan":   "план"     if p.get("jp",0.5)>=0.5 else "эксперимент"
    }

def reflect_emotion(text:str)->str:
    t=(text or "").lower()
    if re.search(r"устал|напряж|тревож|злюсь|злость|раздраж",t): return "Слышу напряжение и заботу о результате. "
    if re.search(r"спокойн|рад|легко|получилось",t): return "Чувствую спокойствие и лёгкость. "
    if re.search(r"не знаю|путаюсь|сомнева",t): return "Вижу, что хочется ясности. "
    return "Я рядом и слышу тебя. "

def open_question(phase:str, style:Dict[str,str])->str:
    if phase=="engage": return "Что сейчас для тебя самое важное?"
    if phase=="focus":  return "На чём тебе хочется остановиться в первую очередь?"
    if phase=="evoke":
        return "Какой смысл ты видишь здесь?" if style["detail"]=="смыслы" else "Какие конкретные шаги ты видишь здесь?"
    if phase=="plan":
        return "Какой маленький шаг ты готова запланировать на сегодня?" if style["plan"]=="план" else "Какой лёгкий эксперимент попробуешь сначала?"
    return "Расскажи немного больше?"

def personalized_reply(uid:int, text:str, phase:str)->str:
    pr = q("SELECT ei,sn,tf,jp,mbti_type FROM psycho_profile WHERE user_id=%s",(uid,))
    p = pr[0] if pr else {"ei":0.5,"sn":0.5,"tf":0.5,"jp":0.5}
    st = comms_style(p)
    return f"{reflect_emotion(text)}{open_question(phase, st)}"

def quality_ok(s:str)->bool:
    if STOP.search(s or ""): return False
    L = len(s or "")
    if L < 90 or L > 350: return False
    if "?" not in (s or ""): return False
    if not re.search(r"(слышу|вижу|понимаю|рядом|важно)", (s or "").lower()): return False
    return True

# -------------------- API --------------------
@app.get("/")
async def root():
    return {"ok":True,"service":"anima"}

@app.post("/webhook/telegram")
async def webhook(update: TelegramUpdate, request: Request):
    # идемпотентность: игнорируем повторные update_id
    upd_id = update.update_id
    if upd_id is not None:
        try:
            q("INSERT INTO processed_updates(update_id) VALUES (%s)", (upd_id,))
        except Exception:
            return {"ok": True}  # дубликат, уже обработан

    if not update.message:
        return {"ok":True}

    msg = update.message
    chat_id = msg["chat"]["id"]
    uid = chat_id
    text = (msg.get("text") or "").strip()
    u = msg.get("from",{})
    ensure_user(uid, u.get("username"), u.get("first_name"), u.get("last_name"))

    # Safety
    if crisis_detect(text):
        reply = ("Я рядом и слышу твою боль. Если нужна поддержка — обратись к близким "
                 "или в службу помощи. Что сейчас было бы самым поддерживающим?")
        await tg_send(chat_id, reply)
        q("INSERT INTO dialog_events(user_id,role,text,mi_phase,emotion,relevance) VALUES(%s,'assistant',%s,'support','tense',false)",(uid,reply))
        return {"ok":True}
    if STOP.search(text):
        reply = "Давай оставим чувствительные темы за рамками. О чём тебе важнее поговорить сейчас?"
        await tg_send(chat_id, reply)
        q("INSERT INTO dialog_events(user_id,role,text,mi_phase,emotion,relevance) VALUES(%s,'assistant',%s,'engage','neutral',false)",(uid,reply))
        return {"ok":True}

    # ---------- Анкета /start ----------
    st = app_state_get(uid)
    if text.lower() in ("/start","старт","начать") or not st.get("kno_done"):
        # первое касание анкеты
        if st.get("kno_idx") is None:
            kno_start(uid)                       # ставит kno_idx = 0
            q1 = KNO[0][1]
            await tg_send(chat_id, INTRO_TEXT)
            await tg_send(chat_id, q1 + SUFFIX)  # первый вопрос
            q("INSERT INTO dialog_events(user_id,role,text,mi_phase) VALUES(%s,'assistant',%s,'engage')",(uid,q1))
            return {"ok": True}                  # ← обязательно выходим после первого вопроса!

        # продолжаем анкету: принимаем текущий ответ и отдаём следующий
        nxt = kno_step(uid, text)
        if nxt is None:
            prof = q("SELECT ei,sn,tf,jp,confidence FROM psycho_profile WHERE user_id=%s",(uid,))[0]
            conf = int((prof["confidence"] or 0)*100)
            reply = (
                "Спасибо, я лучше понимаю, как с тобой говорить 💛\n"
                f"Пока это черновой профиль. Уверенность {conf}% и будет расти по ходу диалога.\n\n"
                "Расскажи коротко — с чем хочешь сегодня поработать или о чём поговорить?"
            )
            await tg_send(chat_id, reply)
            q("INSERT INTO dialog_events(user_id,role,text,mi_phase) VALUES(%s,'assistant',%s,'engage')",(uid,reply))
            return {"ok": True}
        else:
            await tg_send(chat_id, nxt + SUFFIX)
            q("INSERT INTO dialog_events(user_id,role,text,mi_phase) VALUES(%s,'assistant',%s,'engage')",(uid,nxt))
            return {"ok": True}

    # ---------- Основной диалог ----------
    emo  = detect_emotion(text)
    last = q("SELECT mi_phase FROM dialog_events WHERE user_id=%s ORDER BY id DESC LIMIT 1",(uid,))
    last_phase = last[0]["mi_phase"] if last else "engage"
    phase = choose_phase(last_phase, emo, text)

    draft = personalized_reply(uid, text, phase)
    if not quality_ok(draft):
        draft = "Слышу тебя. Что здесь для тебя главное?"

    # логируем сообщение пользователя и ответ
    q("""INSERT INTO dialog_events(user_id,role,text,mi_phase,emotion,relevance)
         VALUES(%s,'user',%s,%s,%s,%s)""",
      (uid, text, phase, emo, False))
    await tg_send(chat_id, draft)
    q("""INSERT INTO dialog_events(user_id,role,text,mi_phase,emotion,relevance)
         VALUES(%s,'assistant',%s,%s,%s,%s)""",
      (uid, draft, phase, emo, False))
    return {"ok":True}

# -------------------- Reports (как было) --------------------
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
        "profile": last_events and prof[0] if prof else {},
        "last_events": last_events or [],
        "quality_14d": quality or []
    }

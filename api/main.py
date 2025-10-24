# api/main.py
import os, re, json
from typing import Any, Dict, Optional, List, Tuple
from fastapi import FastAPI, Request, Header
from pydantic import BaseModel
from dotenv import load_dotenv
import httpx
import psycopg2, psycopg2.extras

# ---------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------
load_dotenv()
app = FastAPI(title="ANIMA 2.0")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
DB_URL         = os.getenv("DATABASE_URL", "")
REPORTS_TOKEN  = os.getenv("REPORTS_TOKEN", "")

# ---------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------
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

def ensure_user(uid:int, username=None, first_name=None, last_name=None):
    q("""
       INSERT INTO user_profile(user_id,username,first_name,last_name)
       VALUES(%s,%s,%s,%s)
       ON CONFLICT (user_id) DO NOTHING
    """,(uid,username,first_name,last_name))

def facts_get(uid:int)->Dict[str,Any]:
    r = q("SELECT facts FROM user_profile WHERE user_id=%s",(uid,))
    return (r[0]["facts"] if r and r[0]["facts"] else {}) or {}

def facts_patch(uid:int, patch:Dict[str,Any]):
    facts = facts_get(uid)
    for k,v in patch.items():
        if isinstance(v, dict) and isinstance(facts.get(k), dict):
            facts[k].update(v)
        else:
            facts[k] = v
    q("UPDATE user_profile SET facts=%s, updated_at=NOW() WHERE user_id=%s",
      (json.dumps(facts), uid))

def app_state_get(uid:int)->Dict[str,Any]:
    f = facts_get(uid)
    return f.get("app_state",{}) if isinstance(f, dict) else {}

def app_state_set(uid:int, patch:Dict[str,Any]):
    st = app_state_get(uid)
    st.update(patch)
    facts_patch(uid, {"app_state": st})

# ---------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------
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

# ---------------------------------------------------------------------
# Safety, guardrails, utilities
# ---------------------------------------------------------------------
STOP   = re.compile(r"(политик|религ|насили|медицинск|вакцин|диагноз|лекарств|суицид)", re.IGNORECASE)
CRISIS = re.compile(r"(не хочу жить|самоповрежд|отчаяни|суицид|покончи|боль невыносима)", re.IGNORECASE)

def crisis_detect(t: str) -> bool:
    return bool(CRISIS.search(t or ""))

def detect_emotion(t: str) -> str:
    tl = (t or "").lower()
    if re.search(r"устал|напряж|тревож|страш|злюсь|злость|раздраж", tl): return "tense"
    if re.search(r"спокойн|рад|легко|хорошо", tl): return "calm"
    if re.search(r"не знаю|путаюсь|сомнева", tl): return "uncertain"
    return "neutral"

# возвращаем на рельсы, если уходим в сторону
def off_topic_guard(user_text:str, focus_topic:Optional[str])->Optional[str]:
    if not focus_topic:
        return None
    tl = (user_text or "").lower()
    # если нет слов из темы и в тексте мало конкретики — мягко возвращаем
    if not any(w in tl for w in focus_topic.lower().split()[:2]):
        return ("Кажется, мы чуть ушли в сторону 🌱\n"
                f"Давай завершим разговор о «{focus_topic}». "
                "Если захочешь сменить тему — скажи «сменим тему на ...».")
    return None

# ---------------------------------------------------------------------
# MI phases (упрощённый FSM)
# ---------------------------------------------------------------------
def choose_phase(last_phase: str, emotion: str, text: str) -> str:
    tl = (text or "").lower()
    if emotion in ("tense","uncertain"):
        return "engage"
    if re.search(r"\bфокус\b|главн|сосредоточ", tl): return "focus"
    if re.search(r"\bпочему\b|\bзачем\b|думаю|хочу понять|кажется", tl): return "evoke"
    if re.search(r"готов|сделаю|попробую|начну|планир", tl): return "plan"
    return "focus" if last_phase=="engage" else last_phase

# ---------------------------------------------------------------------
# Mini-KNO / MBTI (4 вопроса)
# ---------------------------------------------------------------------
KNO = [
    ("ei", "Когда ты устаёшь — что помогает быстрее восстановиться: пообщаться с людьми 🌿 или побыть наедине ☁️?"),
    ("sn", "Что тебе ближе: действовать по конкретным шагам и фактам 📍 или ориентироваться на идею и смысл ✨?"),
    ("tf", "Как ты чаще принимаешь решения: через логику и аргументы 🧠 или через чувства и внутренние ценности 💛?"),
    ("jp", "Когда тебе спокойнее: когда всё чётко спланировано 📋 или когда есть свобода и импровизация 🎈?")
]

AXIS_LABEL = {
    "ei": ("E","I"), "sn": ("S","N"), "tf": ("T","F"), "jp": ("J","P")
}

def kno_start(uid:int):
    app_state_set(uid, {"kno_idx":0, "kno_answers":{}, "kno_done":False})

def _pick_choice(axis:str, text:str)->int:
    t = (text or "").strip().lower()
    if t in {"1","первый","первое","первая"}: return 1
    if t in {"2","второй","второе","вторая"}: return 2
    if axis=="ei":
        if any(w in t for w in ["наедин","тишин","один","одна"]): return 2
        if any(w in t for w in ["люд","общат","встреч","друз"]):   return 1
    if axis=="sn":
        if any(w in t for w in ["факт","конкрет","шаг","пошаг"]):  return 1
        if any(w in t for w in ["смысл","иде","образ","инсайт"]):  return 2
    if axis=="tf":
        if any(w in t for w in ["логик","аргумент","рацио","анал"]): return 1
        if any(w in t for w in ["чувств","эмоци","ценност","сердц"]): return 2
    if axis=="jp":
        if any(w in t for w in ["план","распис","контрол","структ"]): return 1
        if any(w in t for w in ["свобод","импров","спонтан"]):         return 2
    return 1  # по умолчанию

def kno_step(uid:int, text:str)->Optional[str]:
    st = app_state_get(uid)
    idx = st.get("kno_idx",0)
    answers = st.get("kno_answers",{})
    axis, question = KNO[idx]
    choice = _pick_choice(axis, text)
    answers[axis] = choice
    idx += 1

    if idx >= len(KNO):
        # агрегируем и пишем профиль
        axes = {"E":0,"I":0,"S":0,"N":0,"T":0,"F":0,"J":0,"P":0}
        for ax, pick in answers.items():
            a,b = AXIS_LABEL[ax]
            axes[a if pick==1 else b] += 1
        def norm(a,b):
            s = a+b
            return (a/(s or 1.0))
        ei = norm(axes["E"],axes["I"])
        sn = norm(axes["N"],axes["S"])  # N как «1», S как «0»
        tf = norm(axes["T"],axes["F"])
        jp = norm(axes["J"],axes["P"])
        q("""INSERT INTO psycho_profile(user_id,ei,sn,tf,jp,confidence,mbti_type,anchors,state)
             VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
             ON CONFLICT (user_id)
             DO UPDATE SET ei=EXCLUDED.ei,sn=EXCLUDED.sn,tf=EXCLUDED.tf,jp=EXCLUDED.jp,
                           confidence=EXCLUDED.confidence, updated_at=NOW()""",
          (uid, ei, sn, tf, jp, 0.45, None, json.dumps([]), None))
        app_state_set(uid, {"kno_done":True, "kno_idx":None, "kno_answers":answers})
        return None
    else:
        app_state_set(uid, {"kno_idx":idx, "kno_answers":answers})
        return KNO[idx][1]

# ---------------------------------------------------------------------
# Personalization
# ---------------------------------------------------------------------
def to_mbti(ei,sn,tf,jp)->str:
    return ("E" if ei>=0.5 else "I") + ("N" if sn>=0.5 else "S") + \
           ("T" if tf>=0.5 else "F") + ("J" if jp>=0.5 else "P")

def comms_style(p:Dict[str,Any])->Dict[str,str]:
    return {
        "tone":   "активный" if p.get("ei",0.5)>=0.5 else "спокойный",
        "detail": "смыслы"   if p.get("sn",0.5)>=0.5 else "шаги",
        "mind":   "анализ"   if p.get("tf",0.5)>=0.5 else "чувства",
        "plan":   "план"     if p.get("jp",0.5)>=0.5 else "эксперимент"
    }

def reflect_emotion(text:str)->str:
    t=(text or "").lower()
    if re.search(r"устал|напряж|тревож|злюсь|злость|раздраж|непонятно|не знаю",t):
        return "Слышу напряжение и потребность в ясности. "
    if re.search(r"спокойн|рад|легко|получилось",t):
        return "Чувствую спокойствие и ресурс. "
    return "Я рядом и слышу тебя. "

def open_question(phase:str, style:Dict[str,str])->str:
    if phase=="engage": return "Что сейчас для тебя самое важное?"
    if phase=="focus":  return "На чём тебе хочется остановиться в первую очередь?"
    if phase=="evoke":
        return "Какой смысл ты видишь здесь?" if style["detail"]=="смыслы" \
               else "Какие конкретные шаги ты видишь здесь?"
    if phase=="plan":
        return "Какой маленький шаг ты готова запланировать на сегодня?" \
               if style["plan"]=="план" \
               else "Какой лёгкий эксперимент попробуешь сначала?"
    return "Расскажи немного больше?"

def personalized_reply(uid:int, text:str, phase:str)->str:
    pr = q("SELECT ei,sn,tf,jp,mbti_type FROM psycho_profile WHERE user_id=%s",(uid,))
    p = pr[0] if pr else {"ei":0.5,"sn":0.5,"tf":0.5,"jp":0.5}
    st = comms_style(p)
    return f"{reflect_emotion(text)}{open_question(phase, st)}"

def quality_ok(s:str)->bool:
    if STOP.search(s): return False
    L = len(s or "")
    if L < 90 or L > 380: return False
    if "?" not in s: return False
    if not re.search(r"(слышу|вижу|понимаю|рядом|важно)", (s or "").lower()):
        return False
    return True

# ---------------------------------------------------------------------
# API
# ---------------------------------------------------------------------
@app.get("/")
async def root():
    return {"ok":True,"service":"anima"}

@app.post("/webhook/telegram")
async def webhook(update: TelegramUpdate, request: Request):
    if not update.message:
        return {"ok":True}

    msg     = update.message
    chat_id = msg["chat"]["id"]
    uid     = chat_id
    text    = (msg.get("text") or "").strip()
    u       = msg.get("from",{})
    ensure_user(uid, u.get("username"), u.get("first_name"), u.get("last_name"))

    # Crisis / sensitive topics
    if crisis_detect(text):
        reply = ("Я рядом и слышу твою боль. Если нужна срочная поддержка — обратись к близким "
                 "или в службу помощи. Что сейчас было бы самым поддерживающим?")
        await tg_send(chat_id, reply)
        q("""INSERT INTO dialog_events(user_id,role,text,mi_phase,emotion,relevance)
             VALUES(%s,'assistant',%s,'support','tense',false)""",(uid,reply))
        return {"ok":True}
    if STOP.search(text):
        reply = "Давай оставим чувствительные темы за рамками. О чём тебе важнее поговорить сейчас?"
        await tg_send(chat_id, reply)
        q("""INSERT INTO dialog_events(user_id,role,text,mi_phase,emotion,relevance)
             VALUES(%s,'assistant',%s,'engage','neutral',false)""",(uid,reply))
        return {"ok":True}

    # ----- Onboarding states ------------------------------------------------
    st = app_state_get(uid)

    # /start — тёплое приветствие и прозрачность
    if text.lower() in ("/start","start","старт","начать"):
        app_state_set(uid, {"stage":"ask_name", "focus_topic":None, "kno_done":False,
                            "kno_idx":None, "kno_answers":{}})
        greet = (
            "Привет 🌿 Я Анима — твой личный психологический ассистент.\n"
            "Помогаю навести ясность, снизить стресс и наметить шаги вперёд. "
            "Наши разговоры конфиденциальны, никакого спама — только поддержка 💛\n\n"
            "Как мне к тебе обращаться?"
        )
        await tg_send(chat_id, greet)
        q("INSERT INTO dialog_events(user_id,role,text,mi_phase) VALUES(%s,'assistant',%s,'engage')",(uid,greet))
        return {"ok":True}

    # спрашиваем имя
    if st.get("stage") == "ask_name":
        name = text.split()[0][:24] if text else "друг"
        facts_patch(uid, {"profile": {"name": name}})
        app_state_set(uid, {"stage":"ask_feel"})
        await tg_send(chat_id, "Как ты сейчас? Выбери слово, которое ближе: спокойно, напряжённо, растерянно — или опиши по-своему.")
        return {"ok":True}

    # спрашиваем состояние
    if st.get("stage") == "ask_feel":
        facts_patch(uid, {"profile": {"feel": text}})
        app_state_set(uid, {"stage":"ask_goal"})
        await tg_send(chat_id, "Чего бы тебе хотелось от наших разговоров? Больше ясности, поддержки, энергии на действия — что откликается?")
        return {"ok":True}

    # спрашиваем ожидание и предлагаём мини-тест
    if st.get("stage") == "ask_goal":
        facts_patch(uid, {"profile": {"goal": text}})
        app_state_set(uid, {"stage":"kno_intro"})
        intro = (
            "Чтобы мне быть полезнее, задам 4 коротких вопроса. Это займёт меньше минуты 🌿\n"
            "Отвечай цифрой 1 или 2, можно словами."
        )
        first_q = KNO[0][1]
        app_state_set(uid, {"kno_idx":0, "kno_answers":{}, "kno_done":False})
        await tg_send(chat_id, f"{intro}\n\n{first_q}\n\nОтветь 1 или 2, можно словами.")
        return {"ok":True}

    # сам мини-тест
    if st.get("kno_done") is False and st.get("kno_idx") is not None:
        nxt = kno_step(uid, text)
        if nxt is None:
            # тест закончен
            prof = q("SELECT ei,sn,tf,jp FROM psycho_profile WHERE user_id=%s",(uid,))[0]
            mbti = to_mbti(prof["ei"],prof["sn"],prof["tf"],prof["jp"])
            facts_patch(uid, {"profile": {"mbti": mbti}})
            app_state_set(uid, {"stage":"focus_ask","kno_done":True})
            reply = (f"Спасибо, я лучше понимаю, как с тобой говорить 💛\n"
                     f"Пока это черновой профиль: *{mbti}*. Он будет уточняться по ходу диалога.\n\n"
                     "Расскажи коротко — с чем хочешь сегодня поработать или о чём поговорить?")
            await tg_send(chat_id, reply)
            return {"ok":True}
        else:
            await tg_send(chat_id, f"{nxt}\n\nОтветь 1 или 2, можно словами.")
            return {"ok":True}

    # фиксируем сегодняшнюю тему/фокус
    if st.get("stage") == "focus_ask":
        app_state_set(uid, {"stage":"dialog", "focus_topic": text})
        await tg_send(chat_id, "Спасибо, записала 💛 Если захочешь изменить фокус — просто напиши.")
        # провокация первого шага
        await tg_send(chat_id, "Готова продолжать. Какой маленький шаг по этой теме был бы для тебя посильным сегодня?")
        return {"ok":True}

    # ----- Основной диалог --------------------------------------------------
    # рельсы: удерживаем на теме
    rail_hint = off_topic_guard(text, st.get("focus_topic"))
    if rail_hint:
        await tg_send(chat_id, rail_hint)
        q("INSERT INTO dialog_events(user_id,role,text,mi_phase) VALUES(%s,'assistant',%s,'engage')",(uid,rail_hint))
        return {"ok":True}

    emo  = detect_emotion(text)
    last = q("SELECT mi_phase FROM dialog_events WHERE user_id=%s ORDER BY id DESC LIMIT 1",(uid,))
    last_phase = last[0]["mi_phase"] if last else "engage"
    phase = choose_phase(last_phase, emo, text)
    draft = personalized_reply(uid, text, phase)
    if not quality_ok(draft):
        draft = "Слышу тебя. Что здесь для тебя главное?"

    await tg_send(chat_id, draft)
    # логируем
    q("""INSERT INTO dialog_events(user_id,role,text,mi_phase,emotion,relevance)
         VALUES(%s,'user',%s,%s,%s,false)""",(uid, text, phase, emo))
    q("""INSERT INTO dialog_events(user_id,role,text,mi_phase,emotion,relevance)
         VALUES(%s,'assistant',%s,%s,%s,false)""",(uid, draft, phase, emo))

    return {"ok":True}

# ---------------------------------------------------------------------
# Reports (как раньше)
# ---------------------------------------------------------------------
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
    ret = q("SELECT * FROM v_retention_7d")
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

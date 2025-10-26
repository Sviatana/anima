# api/main.py

import os, re, json, time, random
from typing import Any, Dict, Optional, List, Tuple
from fastapi import FastAPI, Request, Header
from pydantic import BaseModel
from dotenv import load_dotenv
import httpx
import psycopg2, psycopg2.extras

# =============== init ==================
load_dotenv()
app = FastAPI(title="ANIMA 2.0")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
DB_URL         = os.getenv("DATABASE_URL", "")
REPORTS_TOKEN  = os.getenv("REPORTS_TOKEN", "")

# =============== DB helpers ============
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

# однократно создаём тех.таблицу
q("""
CREATE TABLE IF NOT EXISTS processed_updates(
  update_id BIGINT PRIMARY KEY,
  created_at TIMESTAMPTZ DEFAULT NOW()
)
""")

# =============== Telegram ==============
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

# =============== Safety =================
STOP   = re.compile(r"(политик|религ|насили|медицинск|вакцин|диагноз|лекарств|суицид)", re.IGNORECASE)
CRISIS = re.compile(r"(не хочу жить|самоповрежд|суицид|покончи|боль невыносима)", re.IGNORECASE)

def crisis_detect(t: str) -> bool:
    return bool(CRISIS.search(t or ""))

# =============== Emotion =================
def detect_emotion(t: str) -> str:
    tl = (t or "").lower()
    if re.search(r"устал|напряж|тревог|страш|злюсь|злость|раздраж|плохо|груст", tl): return "tense"
    if re.search(r"спокойн|рад|легко|класс|хорошо|супер|ок", tl):                  return "calm"
    if re.search(r"не знаю|путаюсь|сомнева|непонятн", tl):                          return "uncertain"
    return "neutral"

# =============== MI Phase FSM ============
def choose_phase(last_phase: str, emotion: str, text: str) -> str:
    tl = (text or "").lower()
    if emotion in ("tense", "uncertain"):
        return "engage"
    if re.search(r"\bфокус\b|главн|сосредоточ", tl): return "focus"
    if re.search(r"\bпочему\b|\bзачем\b|думаю|хочу понять|кажется", tl): return "evoke"
    if re.search(r"готов|сделаю|попробую|начну|планир", tl): return "plan"
    return "focus" if last_phase == "engage" else last_phase

# =============== KNO (mini-MBTI) =========
KNO = [
    ("ei_q1", "Когда ты устаёшь — что помогает быстрее восстановиться: пообщаться с людьми 🌱 или побыть наедине ☁️?"),
    ("sn_q1", "Что тебе ближе: действовать по конкретным шагам и фактам 🎯 или ориентироваться на идею и смысл ✨?"),
    ("tf_q1", "Как ты чаще принимаешь решения: через логику и аргументы 🧠 или через чувства и внутренние ценности 💛?"),
    ("jp_q1", "Когда тебе спокойнее: когда всё чётко спланировано 📋 или когда есть свобода и импровизация 🎲?"),
    ("jp_q2", "Когда много задач: составить список заранее или пробовать и смотреть по ситуации?"),
    ("ei_q2", "Когда нужно разобраться: поговорить с кем-то или записать мысли для себя?")
]
KNO_MAP = {
    "ei_q1": ("E","I"),
    "sn_q1": ("S","N"),
    "tf_q1": ("T","F"),
    "jp_q1": ("J","P"),
    "jp_q2": ("J","P"),
    "ei_q2": ("E","I"),
}

# =============== Profiles & state =========
def ensure_user(uid:int, username=None, first_name=None, last_name=None):
    q("""INSERT INTO user_profile(user_id,username,first_name,last_name)
         VALUES(%s,%s,%s,%s)
         ON CONFLICT (user_id) DO NOTHING""",
      (uid,username,first_name,last_name))

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
    q("UPDATE user_profile SET facts=%s, updated_at=NOW() WHERE user_id=%s",(json.dumps(facts),uid))

def kno_start(uid:int):
    app_state_set(uid, {"kno_idx":0, "kno_answers":{}, "kno_done":False})

def kno_step(uid:int, text:str)->Optional[str]:
    st = app_state_get(uid)
    # если по какой-то причине индекса нет — стартуем
    if st.get("kno_idx") is None:
        st["kno_idx"] = 0
        st["kno_answers"] = {}
        st["kno_done"] = False
    idx = st.get("kno_idx", 0)
    answers = st.get("kno_answers", {})

    t = (text or "").strip().lower()

    def pick_by_keywords(question_key:str, t:str)->int:
        if t in {"1","первый","первое","первая"}: return 1
        if t in {"2","второй","второе","вторая"}: return 2
        if question_key.startswith("ei_"):
            if "наедин" in t or "один" in t or "тишин" in t: return 2
            if "люд" in t or "общат" in t or "встреч" in t:  return 1
        if question_key.startswith("sn_"):
            if "факт" in t or "конкрет" in t or "шаг" in t:   return 1
            if "смысл" in t or "иде" in t or "образ" in t:    return 2
        if question_key.startswith("tf_"):
            if "логик" in t or "рацион" in t or "аргумент" in t: return 1
            if "чувств" in t or "эмоци" in t or "ценност" in t:   return 2
        if question_key.startswith("jp_"):
            if "план" in t or "распис" in t or "контрол" in t: return 1
            if "свобод" in t or "импров" in t or "спонтан" in t: return 2
        return 1

    # если вышли за предел — считаем завершённым
    if idx >= len(KNO):
        return None

    key, _ = KNO[idx]
    choice = pick_by_keywords(key, t)
    answers[key] = choice
    idx += 1

    if idx >= len(KNO):
        # финал: вычисляем “оси”
        axes = {"E":0,"I":0,"S":0,"N":0,"T":0,"F":0,"J":0,"P":0}
        for k,v in answers.items():
            a,b = KNO_MAP[k]
            axes[a if v==1 else b]+=1

        def norm(a,b):
            s=a+b
            return (a/(s or 1), b/(s or 1))

        E,I = norm(axes["E"],axes["I"])
        S,N = norm(axes["S"],axes["N"])
        T,F = norm(axes["T"],axes["F"])
        J,P = norm(axes["J"],axes["P"])

        # сохраняем профиль
        q("""INSERT INTO psycho_profile(user_id,ei,sn,tf,jp,confidence,mbti_type,anchors,state)
             VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
             ON CONFLICT (user_id) DO UPDATE
             SET ei=EXCLUDED.ei,sn=EXCLUDED.sn,tf=EXCLUDED.tf,jp=EXCLUDED.jp,
                 confidence=EXCLUDED.confidence, updated_at=NOW()""",
          (uid,E,N,T,J,0.4,None,json.dumps([]),None))

        app_state_set(uid, {"kno_done":True,"kno_idx":None,"kno_answers":answers})
        return None
    else:
        app_state_set(uid, {"kno_idx":idx,"kno_answers":answers})
        return KNO[idx][1]

# =============== Relevance & profile tune =========
def classify_relevance(t:str)->Tuple[bool,Dict[str,float],List[Dict[str,Any]]]:
    axes, anchors, rel = {}, [], False
    tl = (t or "").lower()
    if re.search(r"планир|расписан|контрол", tl):
        axes["jp"]=axes.get("jp",0)+0.2; anchors.append({"axis":"jp","quote":"планирование"}); rel=True
    if re.search(r"спонтан|импровиз", tl):
        axes["jp"]=axes.get("jp",0)-0.2; anchors.append({"axis":"jp","quote":"спонтанность"}); rel=True
    if re.search(r"встреч|команда|люд(ей|ям)|общаться", tl):
        axes["ei"]=axes.get("ei",0)+0.2; anchors.append({"axis":"ei","quote":"общительность"}); rel=True
    if re.search(r"тишин|один|наедине", tl):
        axes["ei"]=axes.get("ei",0)-0.2; anchors.append({"axis":"ei","quote":"уединение"}); rel=True
    if re.search(r"факты|пошагов|конкретн", tl):
        axes["sn"]=axes.get("sn",0)-0.15; anchors.append({"axis":"sn","quote":"факты"}); rel=True
    if re.search(r"смысл|образ|идея", tl):
        axes["sn"]=axes.get("sn",0)+0.15; anchors.append({"axis":"sn","quote":"смыслы"}); rel=True
    if re.search(r"логик|рацио|сравн", tl):
        axes["tf"]=axes.get("tf",0)+0.15; anchors.append({"axis":"tf","quote":"анализ"}); rel=True
    if re.search(r"чувств|гармони|эмоци", tl):
        axes["tf"]=axes.get("tf",0)-0.15; anchors.append({"axis":"tf","quote":"эмпатия"}); rel=True
    return rel, axes, anchors

def ewma(v:float, delta:float, alpha:float=0.1)->float:
    return max(0.0, min(1.0, v + alpha * delta))

def to_mbti(ei,sn,tf,jp)->str:
    return ("E" if ei>=0.5 else "I")+("N" if sn>=0.5 else "S")+("T" if tf>=0.5 else "F")+("J" if jp>=0.5 else "P")

def update_profile(uid:int, delta:Dict[str,float], anchors:List[Dict[str,Any]]):
    rows = q("SELECT ei,sn,tf,jp,confidence,anchors FROM psycho_profile WHERE user_id=%s",(uid,))
    if not rows:
        ensure_user(uid)
        q("INSERT INTO psycho_profile(user_id) VALUES(%s)",(uid,))
        rows = q("SELECT ei,sn,tf,jp,confidence,anchors FROM psycho_profile WHERE user_id=%s",(uid,))
    p = rows[0]
    ei,sn,tf,jp = p["ei"],p["sn"],p["tf"],p["jp"]
    if "ei" in delta: ei = ewma(ei, delta["ei"])
    if "sn" in delta: sn = ewma(sn, delta["sn"])
    if "tf" in delta: tf = ewma(tf, delta["tf"])
    if "jp" in delta: jp = ewma(jp, delta["jp"])
    conf = min(0.99, (p["confidence"] or 0) + (0.02 if delta else 0.0))
    anc = (p["anchors"] or []) + anchors
    mbti = to_mbti(ei,sn,tf,jp) if conf>=0.4 else None
    q("""UPDATE psycho_profile SET ei=%s,sn=%s,tf=%s,jp=%s,
         confidence=%s,mbti_type=%s,anchors=%s,updated_at=NOW()
         WHERE user_id=%s""",(ei,sn,tf,jp,conf,mbti,json.dumps(anc[-50:]),uid))

# =============== Personalization / NLG =========
def comms_style(p:Dict[str,Any])->Dict[str,str]:
    return {
        "tone":   "активный" if (p or {}).get("ei",0.5)>=0.5 else "спокойный",
        "detail": "смыслы"   if (p or {}).get("sn",0.5)>=0.5 else "шаги",
        "mind":   "анализ"   if (p or {}).get("tf",0.5)>=0.5 else "чувства",
        "plan":   "план"     if (p or {}).get("jp",0.5)>=0.5 else "эксперимент"
    }

def reflect_emotion(text:str)->str:
    t=(text or "").lower()
    if re.search(r"устал|напряж|тревож|злюсь|злость|раздраж|груст",t): return random.choice([
        "Слышу напряжение. ",
        "Понимаю, что сейчас нелегко. ",
        "Кажется, внутри штормит. ",
    ])
    if re.search(r"спокойн|рад|легко|получилось|класс|супер|ок",t): return random.choice([
        "Рада видеть спокойствие. ",
        "Класс, звучит уверенно. ",
        "Супер — есть опора. ",
    ])
    if re.search(r"не знаю|путаюсь|сомнева|непонятн",t): return random.choice([
        "Вижу, хочется ясности. ",
        "Можно растеряться — я рядом. ",
    ])
    return random.choice(["Я рядом и слышу тебя. ","Слышу тебя. ","Понимаю тебя. "])

def humor_seed()->str:
    return random.choice([
        "Могу добавить щепотку юмора — если не против 😊",
        "Иногда помогает лёгкая ирония — скажи, если ок 😉",
        "Если уместно, могу пошутить — только по-доброму 😌",
    ])

def open_question(phase:str, style:Dict[str,str])->str:
    if phase=="engage":
        return random.choice([
            "Что сейчас для тебя самое важное?",
            "С чего начнём — что тревожит больше всего?",
        ])
    if phase=="focus":
        return random.choice([
            "На чём тебе хочется остановиться в первую очередь?",
            "Если сузить фокус — где точка приложения усилий?",
        ])
    if phase=="evoke":
        return "Какой смысл ты видишь здесь?" if style["detail"]=="смыслы" \
            else "Какие конкретные шаги ты видишь здесь?"
    if phase=="plan":
        return "Какой маленький шаг запланируем на сегодня?" if style["plan"]=="план" \
            else "Какой лёгкий эксперимент попробуешь сначала?"
    return "Расскажи чуть больше?"

def personalized_reply(uid:int, text:str, phase:str, allow_humor:bool)->str:
    pr = q("SELECT ei,sn,tf,jp,mbti_type FROM psycho_profile WHERE user_id=%s",(uid,))
    p = pr[0] if pr else {"ei":0.5,"sn":0.5,"tf":0.5,"jp":0.5}
    st = comms_style(p)
    head = reflect_emotion(text)
    tail = open_question(phase, st)
    if allow_humor and phase in ("engage","focus") and random.random()<0.25:
        return f"{head}{tail} {humor_seed()}"
    return f"{head}{tail}"

# =============== Quality gate (мягкий) ========
def quality_ok(s:str, user_text:str)->bool:
    # запретные темы — стоп
    if STOP.search(s or ""):
        return False
    # если пользовательская реплика короткая — разрешаем короткий ответ
    if len((user_text or "")) < 35:
        return True
    # не душим за длину — просто отбрасываем слишком длинное
    if len(s or "") > 600:
        return False
    return True

def smart_fallback(user_text:str)->str:
    t=(user_text or "").lower()
    if re.search(r"уверен",t):   return "Хочешь развить уверенность? Где она особенно нужна сейчас — в делах, отношениях или в себе?"
    if re.search(r"страх|боюсь|тревог",t): return "Понимаю, страх бывает сильным. Что его чаще всего запускает — неопределённость, прошлый опыт или мнение других?"
    if re.search(r"злост|раздраж|злюсь",t):return "Злость — это сигнал о границах. Хочешь вместе понять, где именно они сейчас затронуты?"
    if re.search(r"груст|печаль",t):       return "Грусть — это про ценное, что сейчас не рядом. Что поддержало бы тебя прямо сегодня?"
    return "Слышу тебя 🌿 Расскажи чуть больше — что тебе важно сейчас почувствовать или изменить?"

# =============== API ===========================
@app.get("/")
async def root():
    return {"ok":True,"service":"anima"}

@app.post("/webhook/telegram")
async def webhook(update: TelegramUpdate, request: Request):
    # идемпотентность
    if update.update_id:
        already = q("SELECT 1 FROM processed_updates WHERE update_id=%s",(update.update_id,))
        if already: return {"ok":True}
        q("INSERT INTO processed_updates(update_id) VALUES(%s)",(update.update_id,))

    if not update.message:
        return {"ok":True}

    msg   = update.message
    chat  = msg.get("chat",{})
    chat_id = chat.get("id")
    uid   = chat_id
    text  = (msg.get("text") or "").strip()
    user  = msg.get("from",{}) or {}
    ensure_user(uid, user.get("username"), user.get("first_name"), user.get("last_name"))

    # Safety first
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

    # ----------- onboarding /start & KNO ----------
    st = app_state_get(uid)
    if text.lower() in ("/start","старт","начать") or not st.get("kno_done"):
        # если первый визит — приветствие и старт анкеты
        if st.get("kno_idx") is None and not st.get("kno_done"):
            kno_start(uid)
            greet = (
                "Привет 🌿 Я Анима — твой личный психологический ассистент. Я помогаю навести ясность, "
                "снизить стресс и наметить шаги вперёд. Наши разговоры конфиденциальны, никакого спама — только поддержка 💛\n\n"
                "Небольшая анкета поможет мне подстроиться под тебя (6 вопросов). "
                "Отвечай цифрой 1 или 2, можно своими словами.\n\n"
            )
            await tg_send(chat_id, greet + KNO[0][1])
            q("INSERT INTO dialog_events(user_id,role,text,mi_phase) VALUES(%s,'assistant',%s,'engage')",(uid,greet+KNO[0][1]))
            return {"ok":True}

        # иначе уже в анкете — обрабатываем
        nxt = kno_step(uid, text)
        if nxt is None:
            prof = q("SELECT ei,sn,tf,jp,confidence FROM psycho_profile WHERE user_id=%s",(uid,))[0]
            conf = int((prof["confidence"] or 0)*100)
            mbti_guess = to_mbti(prof["ei"], prof["sn"], prof["tf"], prof["jp"])
            reply = (
                f"Спасибо, я лучше понимаю, как с тобой говорить 💛\n"
                f"Пока это черновой профиль: {mbti_guess}. Уверенность {conf}% и будет расти по мере общения.\n\n"
                "Расскажи коротко — с чем хочешь сегодня поработать или о чём поговорить?"
            )
            await tg_send(chat_id, reply)
            q("INSERT INTO dialog_events(user_id,role,text,mi_phase) VALUES(%s,'assistant',%s,'engage')",(uid,reply))
            return {"ok": True}
        else:
            await tg_send(chat_id, nxt + "\n\nОтветь 1 или 2, можно словами.")
            q("INSERT INTO dialog_events(user_id,role,text,mi_phase) VALUES(%s,'assistant',%s,'engage')",(uid,nxt))
            return {"ok": True}

    # ----------- обычный диалог -----------
    emo = detect_emotion(text)
    rel, axes, anchors = classify_relevance(text)
    if rel:
        update_profile(uid, axes, anchors)

    last = q("SELECT mi_phase FROM dialog_events WHERE user_id=%s ORDER BY id DESC LIMIT 1",(uid,))
    last_phase = last[0]["mi_phase"] if last else "engage"
    phase = choose_phase(last_phase, emo, text)

    # позволим легкий юмор только если не “tense”
    allow_humor = (emo in ("neutral","calm"))
    draft = personalized_reply(uid, text, phase, allow_humor)

    if not quality_ok(draft, text):
        draft = smart_fallback(text)

    # Send & log
    await tg_send(chat_id, draft)
    q("""INSERT INTO dialog_events(user_id,role,text,mi_phase,emotion,relevance,axes)
         VALUES(%s,'user',%s,%s,%s,%s,%s)""",
      (uid, text, phase, emo, rel, json.dumps(axes if rel else {})))
    q("""INSERT INTO dialog_events(user_id,role,text,mi_phase,emotion,relevance)
         VALUES(%s,'assistant',%s,%s,%s,%s)""",
      (uid, draft, phase, emo, rel))
    return {"ok":True}

# =============== Reports (как было) ============
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

import os, re, json
from typing import Any, Dict, Optional, List, Tuple
from fastapi import FastAPI, Request
from pydantic import BaseModel
from dotenv import load_dotenv
import httpx
import psycopg2, psycopg2.extras

load_dotenv()
app = FastAPI(title="ANIMA 2.0")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
DB_URL = os.getenv("DATABASE_URL", "")

# ---------- DB ----------
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

# ---------- Telegram ----------
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

# ---------- Safety ----------
STOP = re.compile(r"(политик|религ|насили|медицинск|вакцин|диагноз|лекарств|суицид)", re.IGNORECASE)
CRISIS = re.compile(r"(не хочу жить|самоповрежд|отчаяни|суицид|покончи|боль невыносима)", re.IGNORECASE)

def crisis_detect(t: str) -> bool: return bool(CRISIS.search(t))

# ---------- Emotion (rule-based) ----------
def detect_emotion(t: str) -> str:
    tl = t.lower()
    if re.search(r"устал|напряж|тревож|страш|злюсь|зла|злость|раздраж", tl): return "tense"
    if re.search(r"спокойн|рад|легко|хорошо", tl): return "calm"
    if re.search(r"не знаю|путаюсь|сомнева", tl): return "uncertain"
    return "neutral"

# ---------- MI Phase FSM ----------
def choose_phase(last_phase: str, emotion: str, text: str) -> str:
    tl = text.lower()
    if emotion in ("tense","uncertain"): return "engage"
    if re.search(r"давай сосредоточим|главное|важнее|сфокус", tl): return "focus"
    if re.search(r"почему|зачем|думаю|хочу понять|кажется", tl): return "evoke"
    if re.search(r"готов|сделаю|попробую|начну|планирую", tl): return "plan"
    return last_phase or "engage"

# ---------- KNO ----------
KNO = [
    ("ei_q1", "Как тебе легче восстанавливаться: пообщаться с людьми или побыть наедине"),
    ("sn_q1", "Что ближе: конкретные шаги и факты или общая идея и смысл"),
    ("tf_q1", "Как чаще принимаешь решения: логика и аргументы или ощущения и ценности"),
    ("jp_q1", "Что спокойнее: чёткий план или свобода и импровизация"),
    ("jp_q2", "Когда много задач: список заранее или пробовать и смотреть по ситуации"),
    ("ei_q2", "Когда нужно разобраться: поговорить с кем-то или записать мысли для себя")
]
KNO_MAP = {"ei_q1":("E","I"), "sn_q1":("S","N"), "tf_q1":("T","F"), "jp_q1":("J","P"), "jp_q2":("J","P"), "ei_q2":("E","I")}

def ensure_user(uid:int, username=None, first_name=None, last_name=None):
    q("""INSERT INTO user_profile(user_id,username,first_name,last_name)
         VALUES(%s,%s,%s,%s) ON CONFLICT (user_id) DO NOTHING""",
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
    app_state_set(uid, {"kno_idx":0, "kno_answers":{}})

def kno_step(uid:int, text:str)->Optional[str]:
    st = app_state_get(uid)
    idx = st.get("kno_idx",0)
    answers = st.get("kno_answers",{})
    a2 = bool(re.search(r"(втор|вторая|второе|или втор)", text.lower()))
    key,_ = KNO[idx]
    answers[key] = 2 if a2 else 1
    idx += 1
    if idx >= len(KNO):
        # compute axes
        axes = {"E":0,"I":0,"S":0,"N":0,"T":0,"F":0,"J":0,"P":0}
        for k,v in answers.items():
            a,b = KNO_MAP[k]
            axes[a if v==1 else b]+=1
        def norm(a,b): s=a+b; return ((a/(s or 1)), (b/(s or 1)))
        E,I = norm(axes["E"],axes["I"]); S,N = norm(axes["S"],axes["N"])
        T,F = norm(axes["T"],axes["F"]); J,P = norm(axes["J"],axes["P"])
        q("""INSERT INTO psycho_profile(user_id,ei,sn,tf,jp,confidence,mbti_type,anchors,state)
             VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
             ON CONFLICT (user_id) DO UPDATE SET ei=EXCLUDED.ei,sn=EXCLUDED.sn,tf=EXCLUDED.tf,jp=EXCLUDED.jp,confidence=EXCLUDED.confidence,updated_at=NOW()""",
          (uid,E,N,T,J,0.4,None,json.dumps([]),None))
        app_state_set(uid, {"kno_done":True,"kno_idx":None,"kno_answers":answers})
        return None
    else:
        app_state_set(uid, {"kno_idx":idx,"kno_answers":answers})
        return KNO[idx][1]

# ---------- Relevance & MBTI update ----------
def classify_relevance(t:str)->Tuple[bool,Dict[str,float],List[Dict[str,Any]]]:
    axes, anchors, rel = {}, [], False
    tl = t.lower()
    if re.search(r"планир|расписан|контролир", tl): axes["jp"]=axes.get("jp",0)+0.2; anchors.append({"axis":"jp","quote":"планирование"}); rel=True
    if re.search(r"спонтан|импровиз", tl):       axes["jp"]=axes.get("jp",0)-0.2; anchors.append({"axis":"jp","quote":"спонтанность"}); rel=True
    if re.search(r"встреч|команда|люд(ей|ям)|общаться", tl): axes["ei"]=axes.get("ei",0)+0.2; anchors.append({"axis":"ei","quote":"общительность"}); rel=True
    if re.search(r"тишин|один|наедине", tl):    axes["ei"]=axes.get("ei",0)-0.2; anchors.append({"axis":"ei","quote":"уединение"}); rel=True
    if re.search(r"факты|пошагов|конкретн", tl):axes["sn"]=axes.get("sn",0)-0.15; anchors.append({"axis":"sn","quote":"факты"}); rel=True
    if re.search(r"смысл|образ|идея", tl):      axes["sn"]=axes.get("sn",0)+0.15; anchors.append({"axis":"sn","quote":"смыслы"}); rel=True
    if re.search(r"логик|рацио|сравн", tl):     axes["tf"]=axes.get("tf",0)+0.15; anchors.append({"axis":"tf","quote":"анализ"}); rel=True
    if re.search(r"чувств|гармони|эмоци", tl):  axes["tf"]=axes.get("tf",0)-0.15; anchors.append({"axis":"tf","quote":"эмпатия"}); rel=True
    return rel, axes, anchors

def ewma(v:float, delta:float, alpha:float=0.1)->float:
    return max(0.0, min(1.0, v + alpha * delta))

def to_mbti(ei,sn,tf,jp)->str:
    return ("E" if ei>=0.5 else "I")+("N" if sn>=0.5 else "S")+("T" if tf>=0.5 else "F")+("J" if jp>=0.5 else "P")

def update_profile(uid:int, delta:Dict[str,float], anchors:List[Dict[str,Any]]):
    rows = q("SELECT ei,sn,tf,jp,confidence,anchors FROM psycho_profile WHERE user_id=%s",(uid,))
    if not rows:
        ensure_user(uid); q("INSERT INTO psycho_profile(user_id) VALUES(%s)",(uid,)); rows = q("SELECT ei,sn,tf,jp,confidence,anchors FROM psycho_profile WHERE user_id=%s",(uid,))
    p = rows[0]; ei,sn,tf,jp = p["ei"],p["sn"],p["tf"],p["jp"]
    if "ei" in delta: ei = ewma(ei, delta["ei"])
    if "sn" in delta: sn = ewma(sn, delta["sn"])
    if "tf" in delta: tf = ewma(tf, delta["tf"])
    if "jp" in delta: jp = ewma(jp, delta["jp"])
    conf = min(0.99, p["confidence"] + (0.02 if delta else 0.0))
    anc = (p["anchors"] or []) + anchors
    mbti = to_mbti(ei,sn,tf,jp) if conf>=0.4 else None
    q("""UPDATE psycho_profile SET ei=%s,sn=%s,tf=%s,jp=%s,confidence=%s,mbti_type=%s,anchors=%s,updated_at=NOW()
         WHERE user_id=%s""",(ei,sn,tf,jp,conf,mbti,json.dumps(anc[-50:]),uid))

# ---------- Personalization ----------
def comms_style(p:Dict[str,Any])->Dict[str,str]:
    style={}
    style["tone"]   = "активный" if p.get("ei",0.5)>=0.5 else "спокойный"
    style["detail"] = "смыслы"   if p.get("sn",0.5)>=0.5 else "шаги"
    style["mind"]   = "анализ"   if p.get("tf",0.5)>=0.5 else "чувства"
    style["plan"]   = "план"     if p.get("jp",0.5)>=0.5 else "эксперимент"
    return style

def reflect_emotion(text:str)->str:
    t=text.lower()
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

# ---------- Quality Gate ----------
def quality_ok(s:str)->bool:
    if STOP.search(s): return False
    if len(s)<30 or len(s)>350: return False
    if "?" not in s: return False
    return True

# ---------- API ----------
@app.get("/")
async def root(): return {"ok":True,"service":"anima"}

@app.post("/webhook/telegram")
async def webhook(update: TelegramUpdate, request: Request):
    if not update.message: return {"ok":True}
    msg = update.message
    chat_id = msg["chat"]["id"]; uid = chat_id
    text = (msg.get("text") or "").strip()
    u = msg.get("from",{})
    ensure_user(uid, u.get("username"), u.get("first_name"), u.get("last_name"))

    # Crisis / safety
    if crisis_detect(text):
        reply = "Я рядом и слышу твою боль. Если нужна немедленная поддержка обратись к близким или в службу помощи. Что сейчас было бы для тебя самым поддерживающим?"
        await tg_send(chat_id, reply)
        q("INSERT INTO dialog_events(user_id,role,text,mi_phase,emotion,relevance) VALUES(%s,'assistant',%s,'support','tense',false)",(uid,reply))
        return {"ok":True}
    if STOP.search(text):
        reply = "Давай оставим чувствительные темы за рамками. О чём тебе важнее поговорить сейчас?"
        await tg_send(chat_id, reply)
        q("INSERT INTO dialog_events(user_id,role,text,mi_phase,emotion,relevance) VALUES(%s,'assistant',%s,'engage','neutral',false)",(uid,reply))
        return {"ok":True}

    # KNO onboarding
    st = app_state_get(uid)
    if text.lower() in ("/start","старт","начать") or not st.get("kno_done"):
        if st.get("kno_idx") is None and not st.get("kno_done"): kno_start(uid)
        if st.get("kno_answers") is None or st.get("kno_idx",0)==0 and not st.get("kno_answers"):
            q1 = KNO[0][1]
            await tg_send(chat_id, f"Привет, я Анима. Давай познакомимся. {q1}")
            q("INSERT INTO dialog_events(user_id,role,text,mi_phase) VALUES(%s,'assistant',%s,'engage')",(uid,q1))
            return {"ok":True}
        nxt = kno_step(uid, text)
        if nxt is None:
            prof = q("SELECT ei,sn,tf,jp,confidence FROM psycho_profile WHERE user_id=%s",(uid,))[0]
            conf = int(prof["confidence"]*100)
            reply = f"Это моё первое впечатление. Уверенность {conf}% и будет расти по мере общения. Готова перейти к свободному диалогу."
            await tg_send(chat_id, reply)
            q("INSERT INTO dialog_events(user_id,role,text,mi_phase) VALUES(%s,'assistant',%s,'engage')",(uid,reply))
            return {"ok":True}
        else:
            await tg_send(chat_id, nxt)
            q("INSERT INTO dialog_events(user_id,role,text,mi_phase) VALUES(%s,'assistant',%s,'engage')",(uid,nxt))
            return {"ok":True}

    # Emotion and relevance
    emo = detect_emotion(text)
    rel, axes, anchors = classify_relevance(text)
    if rel: update_profile(uid, axes, anchors)

    # Phase and personalized reply
    last = q("SELECT mi_phase FROM dialog_events WHERE user_id=%s ORDER BY id DESC LIMIT 1",(uid,))
    last_phase = last[0]["mi_phase"] if last else "engage"
    phase = choose_phase(last_phase, emo, text)
    draft = personalized_reply(uid, text, phase)
    if not quality_ok(draft): draft = "Слышу тебя. Что здесь для тебя главное?"

    # Send and log
    await tg_send(chat_id, draft)
    q("INSERT INTO dialog_events(user_id,role,text,mi_phase,emotion,relevance,axes) VALUES(%s,'user',%s,%s,%s,%s,%s)",
      (uid,text,phase,emo,rel,json.dumps(axes if rel else {})))
    q("INSERT INTO dialog_events(user_id,role,text,mi_phase,emotion,relevance) VALUES(%s,'assistant',%s,%s,%s,%s)",
      (uid,draft,phase,emo,rel))
    return {"ok":True}

@app.post("/jobs/daily-topics")
async def daily_topics(payload: Dict[str, Any] = None):
    topics = [
        {"title":"Где тебе сейчас нужна поддержка","why":"заметила акцент на ответственности"},
        {"title":"Что помогает тебе восстанавливаться","why":"важно укреплять ресурс"},
        {"title":"Как ты узнаёшь, что тебе спокойно","why":"сигналы спокойствия"}
    ]
    return {"topics": topics}

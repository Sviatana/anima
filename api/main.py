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

# one-time DDL (safe if exists)
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

# very light sentiment cues
def detect_emotion(t: str) -> str:
    tl = (t or "").lower()
    if re.search(r"устал|напряж|тревож|страш|злюсь|злость|раздраж|грустн|плохо", tl): return "tense"
    if re.search(r"спокойн|рад|легко|хорошо|класс", tl): return "calm"
    if re.search(r"не знаю|путаюсь|сомнева|непонятно|не понимаю", tl): return "uncertain"
    return "neutral"

# small “quality gate” to avoid dry/short replies
def quality_score(user_text: str, reply: str) -> float:
    s = 0.0
    L = len(reply or "")
    if 90 <= L <= 500: s += 0.25
    if "?" in (reply or ""): s += 0.25
    if re.search(r"(слышу|вижу|понимаю|рядом|важно|чувствую)", (reply or "").lower()):
        s += 0.25
    # reflect a significant word back
    tokens = [w for w in re.findall(r"[а-яa-z]{4,}", (user_text or "").lower()) if w not in {"сейчас","просто","очень","хочу"}]
    if any(t in (reply or "").lower() for t in tokens[:5]): s += 0.25
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
    if idx >= len(KNO):
        return None
    return KNO[idx][1] + "\n\nОтветь 1 или 2, можно словами."

def kno_register(uid:int, text:str)->Optional[str]:
    st = app_state(uid)
    idx = st.get("kno_idx", 0)
    if idx is None: return None
    if idx >= len(KNO):
        return None

    key,_ = KNO[idx]
    # normalize choice
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
        # finalize
        axes = {"E":0,"I":0,"S":0,"N":0,"T":0,"F":0,"J":0,"P":0}
        for k,v in answers.items():
            a,b = KNO_MAP[k]
            axes[a if v==1 else b]+=1
        def norm(a,b): s=a+b; return (a/(s or 1), b/(s or 1))
        E,I = norm(axes["E"],axes["I"]); S,N = norm(axes["S"],axes["N"])
        T,F = norm(axes["T"],axes["F"]); J,P = norm(axes["J"],axes["P"])
        # upsert profile
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
    if re.search(r"устал|напряж|тревож|злюсь|грустн|плохо",t): return "Слышу напряжение и заботу о результате. "
    if re.search(r"спокойн|рад|легко|класс|хорошо",t): return "Чувствую спокойствие и лёгкость. "
    if re.search(r"не знаю|путаюсь|сомнева|непонятно",t): return "Вижу, что хочется ясности. "
    return "Я рядом и слышу тебя. "

def focus_question(style:Dict[str,str])->str:
    if style["detail"]=="смыслы":
        return "Что здесь для тебя главное?"
    return "Какие конкретные шаги ты видишь здесь?"

def step_question(style:Dict[str,str])->str:
    if style["plan"]=="план":
        return "Какой маленький шаг ты готова наметить на сегодня?"
    return "Какой лёгкий эксперимент попробуешь сначала?"

def playful_addon(humor_on: bool)->str:
    return " (чуть-чуть иронии не повредит 😉)" if humor_on else ""

def build_reply(uid:int, user_text:str, humor_on:bool)->str:
    pr = q("SELECT ei,sn,tf,jp,mbti_type FROM psycho_profile WHERE user_id=%s",(uid,))
    p = pr[0] if pr else {"ei":0.5,"sn":0.5,"tf":0.5,"jp":0.5}
    st = comms_style(p)

    # если пользователь явно задал вопрос — отвечаем по делу + фокус
    if re.search(r"\?$", user_text.strip()) or re.search(r"(как|что|зачем|почему)\b", user_text.lower()):
        return (
            f"{reflect_emotion(user_text)}Попробую коротко и по делу{playful_addon(humor_on)}. "
            f"{focus_question(st)}\n\n"
            f"{step_question(st)}"
        )

    # иначе — стандартная коучинговая связка
    return (
        f"{reflect_emotion(user_text)}Чтобы продвинуться по теме — "
        f"выдели 5–10 минут и выпиши 3 шага/мысли. Какой из них попробуешь сегодня? "
        f"Если хочется, могу добавить щепотку юмора — просто напиши «пошути»."
    )

# -------------------- API --------------------
@app.get("/")
async def root():
    return {"ok":True,"service":"anima"}

@app.post("/webhook/telegram")
async def webhook(update: TelegramUpdate, request: Request):
    # idempotency
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

    # commands for humor mode
    if text.lower().startswith("/humor"):
        on = any(w in text.lower() for w in ["on","вкл","да"])
        st = app_state(uid)
        st["humor_on"] = on
        set_state(uid, st)
        await tg_send(chat_id, "Юмор включён 😊" if on else "Юмор выключен 👍")
        return {"ok":True}

    if re.search(r"\bпошути|немного юмора|чуть иронии\b", text.lower()):
        st = app_state(uid); st["humor_on"] = True; set_state(uid, st)

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
    st = app_state(uid)
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
            # treat message as a name (коротко и без цифр)
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
            # mark intro complete and start KNO
            set_state(uid, {"intro_done":True})
            await tg_send(chat_id, "Спасибо! Начнём с короткой анкеты (6 вопросов). Отвечай 1 или 2, можно словами.")
            kno_start(uid)
            nxt = kno_next(uid)
            await tg_send(chat_id, nxt)
            return {"ok":True}

    # KNO flow if not done
    if not st.get("kno_done"):
        nxt = kno_register(uid, text)
        if nxt is None:
            # finished — отправим резюме и перейдём к свободному диалогу
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

    # quality safety net
    if quality_score(text, draft) < 0.75:
        draft = (f"{reflect_emotion(text)}Чтобы мне быть полезнее — скажи в одном-двух предложениях, "
                 f"что здесь для тебя главное. Затем подберём шаг на сегодня.")

    await tg_send(chat_id, draft)

    # log
    q("""INSERT INTO dialog_events(user_id,role,text,mi_phase,emotion,relevance)
         VALUES(%s,'user',%s,'engage',%s,true)""",(uid,text,emo))
    q("""INSERT INTO dialog_events(user_id,role,text,mi_phase,emotion,relevance)
         VALUES(%s,'assistant',%s,'engage',%s,true)""",(uid,draft,emo))

    return {"ok":True}

# api/main.py
import os, re, json, hashlib
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
DB_URL         = os.getenv("DATABASE_URL", "")
REPORTS_TOKEN  = os.getenv("REPORTS_TOKEN", "")

# -------------- DB helpers --------------
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

# идемпотентность апдейтов
q("""
CREATE TABLE IF NOT EXISTS processed_updates (
  update_id BIGINT PRIMARY KEY,
  created_at TIMESTAMPTZ DEFAULT NOW()
)""")

# -------------- Telegram --------------
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

# -------------- Safety --------------
STOP   = re.compile(r"(политик|религ|насили|медицинск|вакцин|диагноз|лекарств|суицид)", re.IGNORECASE)
CRISIS = re.compile(r"(не хочу жить|самоповрежд|отчаяни|суицид|покончи|боль невыносима)", re.IGNORECASE)

def crisis_detect(t: str) -> bool:
    return bool(CRISIS.search(t or ""))

# -------------- Emotion --------------
def detect_emotion(t: str) -> str:
    tl = (t or "").lower()
    if re.search(r"устал|напряж|тревож|страш|злюсь|злость|раздраж|груст", tl): return "tense"
    if re.search(r"спокойн|рад|легко|хорошо|класс", tl): return "calm"
    if re.search(r"не знаю|путаюсь|сомнева|непонят", tl): return "uncertain"
    return "neutral"

# -------------- MI Phase FSM --------------
def choose_phase(last_phase: str, emotion: str, text: str) -> str:
    tl = (text or "").lower()
    if emotion in ("tense", "uncertain"):
        return "engage"
    if re.search(r"\bфокус\b|главн|сосредоточ", tl): return "focus"
    if re.search(r"\bпочему\b|\bзачем\b|думаю|хочу понять|кажется", tl): return "evoke"
    if re.search(r"готов|сделаю|попробую|начну|планир|шаг", tl): return "plan"
    return "focus" if last_phase == "engage" else last_phase

# -------------- Короткая анкета (КНО) --------------
KNO: List[Tuple[str, str]] = [
    ("ei_q1", "Когда ты устаёшь — что помогает быстрее восстановиться: пообщаться с людьми 🌿 или побыть наедине ☁️?"),
    ("sn_q1", "Что тебе ближе: действовать по конкретным шагам и фактам 🧭 или ориентироваться на идею и смысл ✨?"),
    ("tf_q1", "Как ты чаще принимаешь решения: через логику и аргументы 🧠 или через чувства и внутренние ценности 💛?"),
    ("jp_q1", "Когда тебе спокойнее: когда всё чётко спланировано 📋 или когда есть свобода и импровизация 🎭?"),
    ("jp_q2", "Когда много задач: составить список заранее или пробовать и смотреть по ситуации?"),
    ("ei_q2", "Когда нужно разобраться: поговорить с кем-то или записать мысли для себя?"),
]
KNO_MAP = {"ei_q1":("E","I"), "sn_q1":("N","S"), "tf_q1":("T","F"), "jp_q1":("J","P"), "jp_q2":("J","P"), "ei_q2":("E","I")}

def ensure_user(uid:int, username=None, first_name=None, last_name=None):
    q("""INSERT INTO user_profile(user_id,username,first_name,last_name)
         VALUES(%s,%s,%s,%s)
         ON CONFLICT (user_id) DO NOTHING""",
      (uid,username,first_name,last_name))

def _get_facts(uid:int)->Dict[str,Any]:
    r = q("SELECT facts FROM user_profile WHERE user_id=%s",(uid,))
    return r[0]["facts"] if r and r[0]["facts"] else {}

def app_state_get(uid:int)->Dict[str,Any]:
    facts = _get_facts(uid)
    return facts.get("app_state",{})

def app_state_patch(uid:int, patch:Dict[str,Any]):
    facts = _get_facts(uid)
    st = facts.get("app_state",{})
    st.update(patch)
    facts["app_state"] = st
    q("UPDATE user_profile SET facts=%s, updated_at=NOW() WHERE user_id=%s",(json.dumps(facts),uid))

def kno_start(uid:int):
    app_state_patch(uid, {"kno_idx":0, "kno_answers":{}})

def _normalize_choice(question_key:str, t:str)->int:
    t = (t or "").strip().lower()
    if t in {"1","первый","первое","первая","левый","левая"}: return 1
    if t in {"2","второй","второе","вторая","правый","правая"}: return 2
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
    if idx is None:
        return None

    answers = st.get("kno_answers",{})

    if idx < len(KNO):
        key,_ = KNO[idx]
        answers[key] = _normalize_choice(key, text)
        idx += 1

    if idx >= len(KNO):
        axes = {"E":0,"I":0,"N":0,"S":0,"T":0,"F":0,"J":0,"P":0}
        for k,v in answers.items():
            a,b = KNO_MAP[k]
            axes[a if v==1 else b]+=1

        def share(a,b):
            s = a+b
            return (a/(s or 1), b/(s or 1))

        E,I = share(axes["E"],axes["I"])
        N,S = share(axes["N"],axes["S"])
        T,F = share(axes["T"],axes["F"])
        J,P = share(axes["J"],axes["P"])

        q("""INSERT INTO psycho_profile(user_id,ei,sn,tf,jp,confidence,mbti_type,anchors,state)
             VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
             ON CONFLICT (user_id) DO UPDATE
               SET ei=EXCLUDED.ei, sn=EXCLUDED.sn, tf=EXCLUDED.tf, jp=EXCLUDED.jp,
                   confidence=EXCLUDED.confidence, updated_at=NOW()""",
          (uid,E,N,T,J,0.40,None,json.dumps([]),None))

        app_state_patch(uid, {"kno_done":True,"kno_idx":None,"kno_answers":answers})
        return None
    else:
        app_state_patch(uid, {"kno_idx":idx,"kno_answers":answers})
        return KNO[idx][1]

# -------------- Relevance & MBTI update --------------
def classify_relevance(t:str)->Tuple[bool,Dict[str,float],List[Dict[str,Any]]]:
    axes, anchors, rel = {}, [], False
    tl = (t or "").lower()
    if re.search(r"планир|расписан|контролир", tl):
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
    return max(0.0, min(1.0, (v if v is not None else 0.5) + alpha * delta))

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

# -------------- Dialog personalization --------------
def comms_style(p:Dict[str,Any])->Dict[str,str]:
    return {
        "tone":   "активный" if (p.get("ei",0.5) or 0.5)>=0.5 else "спокойный",
        "detail": "смыслы"   if (p.get("sn",0.5) or 0.5)>=0.5 else "шаги",
        "mind":   "анализ"   if (p.get("tf",0.5) or 0.5)>=0.5 else "чувства",
        "plan":   "план"     if (p.get("jp",0.5) or 0.5)>=0.5 else "эксперимент"
    }

def reflect_emotion(text:str)->str:
    t=(text or "").lower()
    if re.search(r"устал|напряж|тревож|злюсь|злость|раздраж|груст",t): return "Слышу напряжение и заботу. "
    if re.search(r"спокойн|рад|легко|получилось|класс",t): return "Чувствую спокойствие и лёгкость. "
    if re.search(r"не знаю|путаюсь|сомнева|непонят",t): return "Вижу, что хочется ясности. "
    return "Я рядом и слышу тебя. "

# Варианты, чтобы не повторяться слово в слово
ASK_VARIANTS = [
    "На чём тебе хочется остановиться в первую очередь?",
    "Что здесь для тебя самое важное?",
    "Если выбрать один фокус — что это будет?",
    "Где точка приложения усилий прямо сейчас?"
]
def vary_prompt(seed:str)->str:
    h = int(hashlib.md5(seed.encode()).hexdigest(),16)
    return ASK_VARIANTS[h % len(ASK_VARIANTS)]

def open_question(phase:str, style:Dict[str,str])->str:
    if phase=="engage": return vary_prompt("engage")
    if phase=="focus":  return vary_prompt("focus")
    if phase=="evoke":
        return "Какой смысл ты видишь здесь?" if style["detail"]=="смыслы" else "Какие конкретные шаги видишь здесь?"
    if phase=="plan":
        return "Какой маленький шаг запланируем на сегодня?" if style["plan"]=="план" else "Какой лёгкий эксперимент попробуем сначала?"
    return "Расскажи немного больше?"

def personalized_reply(uid:int, text:str, phase:str)->str:
    pr = q("SELECT ei,sn,tf,jp,mbti_type FROM psycho_profile WHERE user_id=%s",(uid,))
    p = pr[0] if pr else {"ei":0.5,"sn":0.5,"tf":0.5,"jp":0.5}
    st = comms_style(p)
    emoji = {"активный":"💪","спокойный":"🌿"}[st["tone"]]
    return f"{reflect_emotion(text)}{open_question(phase, st)} {emoji}"

# -------------- Quality Gate --------------
def quality_ok(s:str)->bool:
    if STOP.search(s): return False
    L = len(s or "")
    if L < 60 or L > 420: return False
    if "?" not in s: return False
    if not re.search(r"(слышу|вижу|понимаю|рядом|важно|давай|попробуем)", (s or "").lower()):
        return False
    return True

# -------------- Light NLU: intents & topics --------------
TOPIC_PATTERNS = {
    "уверенность": r"уверенн|самооцен|сомнен",
    "стресс": r"стресс|тревог|напряж",
    "отношения": r"отношен|муж|парн|жен|развод|ссора",
    "работа": r"работ|карьер|коллег|началь",
    "мотивация": r"мотивац|лень|прокрастин",
    "настроение": r"груст|апат|радост|злост",
    "сон": r"сон|бессон",
    "цели": r"цель|план|фокус",
}

def detect_topic(t:str)->Optional[str]:
    tl=(t or "").lower()
    for name, pat in TOPIC_PATTERNS.items():
        if re.search(pat, tl): return name
    return None

def parse_yes_no(t:str)->Optional[bool]:
    tl=(t or "").lower().strip()
    if tl in {"да","ага","угу","конечно","ок","давай","попробуй","не против","согласен","согласна"}: return True
    if tl in {"нет","не","неа","не надо","не хочу"}: return False
    return None

def wants_humor(t:str)->Optional[bool]:
    tl=(t or "").lower()
    if re.search(r"пошут|юмор|шутк", tl): return True
    yn = parse_yes_no(tl)
    return yn

def wants_examples_or_plan(t:str)->bool:
    tl=(t or "").lower()
    return bool(re.search(r"пример|как|что делать|с чего начать|план|шаг|совет", tl))

def is_unknown(t:str)->bool:
    return bool(re.search(r"не знаю|непонят|сложно сказать|затрудняюсь", (t or "").lower()))

# -------------- API --------------
WELCOME = (
    "Привет 🌿 Я Анима — твой личный психологический ассистент. "
    "Помогаю навести ясность, снизить стресс и наметить шаги вперёд. "
    "Наши разговоры конфиденциальны, никакого спама — только поддержка 💛\n\n"
    "Чтобы быть полезнее, начнём с короткой анкеты (6 вопросов). Отвечай цифрой 1 или 2, можно своими словами.\n\n"
)

@app.get("/")
async def root():
    return {"ok":True,"service":"anima"}

@app.get("/healthz")
async def healthz():
    return {"ok":True}

def allow_reports(x_token:str)->bool:
    return (REPORTS_TOKEN == "" or REPORTS_TOKEN == x_token)

@app.get("/reports/summary")
async def reports_summary(x_token: str = Header(default="")):
    if not allow_reports(x_token):
        return {"error": "unauthorized"}
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
    return {"kpi": kpi[0] if kpi else {}, "confidence_hist": conf or [], "retention7d": ret[0] if ret else {}}

# вспомогательная отправка с защитой от повторов одного и того же промпта подряд
def set_last_prompt(uid:int, text:str):
    h = hashlib.md5((text or "").encode()).hexdigest()
    app_state_patch(uid, {"last_prompt_hash": h})

def is_same_prompt(uid:int, text:str)->bool:
    st = app_state_get(uid)
    h = hashlib.md5((text or "").encode()).hexdigest()
    return st.get("last_prompt_hash")==h

@app.post("/webhook/telegram")
async def webhook(update: TelegramUpdate, request: Request):
    # идемпотентность
    if update.update_id is not None:
        try:
            q("INSERT INTO processed_updates(update_id) VALUES(%s) ON CONFLICT DO NOTHING",(update.update_id,))
            seen = q("SELECT 1 FROM processed_updates WHERE update_id=%s",(update.update_id,))
            if not seen:
                return {"ok":True}
        except Exception as e:
            print("idempotency err", e)

    if not update.message:
        return {"ok":True}

    msg     = update.message
    chat_id = msg["chat"]["id"]
    uid     = chat_id
    text    = (msg.get("text") or "").strip()
    u       = msg.get("from",{})
    ensure_user(uid, u.get("username"), u.get("first_name"), u.get("last_name"))

    # старт / анкета
    st = app_state_get(uid)
    if text.lower() in ("/start","старт","начать") or not st.get("kno_done"):
        if st.get("kno_idx") is None or st.get("kno_idx") == 0 and not st.get("kno_answers"):
            kno_start(uid)
            first = WELCOME + KNO[0][1] + "\n\nОтветь 1 или 2, можно словами."
            await tg_send(chat_id, first)
            q("INSERT INTO dialog_events(user_id,role,text,mi_phase) VALUES(%s,'assistant',%s,'engage')",(uid,KNO[0][1]))
            set_last_prompt(uid, KNO[0][1])
            return {"ok":True}

        nxt = kno_step(uid, text)
        if nxt is None:
            prof = q("SELECT ei,sn,tf,jp,confidence,mbti_type FROM psycho_profile WHERE user_id=%s",(uid,))[0]
            conf = int((prof["confidence"] or 0)*100)
            mbti = prof["mbti_type"] or "—"
            about = (f"Спасибо, я лучше понимаю, как с тобой говорить 💛\n"
                     f"Пока это черновой профиль: {mbti}. Уверенность {conf}% и будет расти по мере общения.\n\n"
                     f"О чём хочешь сегодня поговорить или поработать? Например: уверенность, стресс, отношения, мотивация.")
            await tg_send(chat_id, about)
            q("INSERT INTO dialog_events(user_id,role,text,mi_phase) VALUES(%s,'assistant',%s,'engage')",(uid,about))
            set_last_prompt(uid, about)
            return {"ok":True}
        else:
            reply = nxt + "\n\nОтвет 1 или 2, можно словами."
            await tg_send(chat_id, reply)
            q("INSERT INTO dialog_events(user_id,role,text,mi_phase) VALUES(%s,'assistant',%s,'engage')",(uid,nxt))
            set_last_prompt(uid, reply)
            return {"ok":True}

    # Safety
    if crisis_detect(text):
        reply = ("Я рядом и слышу твою боль. Если нужна поддержка — обратись к близким "
                 "или в службу помощи. Что сейчас было бы самым поддерживающим?")
        await tg_send(chat_id, reply)
        q("INSERT INTO dialog_events(user_id,role,text,mi_phase,emotion,relevance) VALUES(%s,'assistant',%s,'support','tense',false)",(uid,reply))
        set_last_prompt(uid, reply)
        return {"ok":True}
    if STOP.search(text):
        reply = "Давай оставим чувствительные темы за рамками. О чём тебе важнее поговорить сейчас?"
        await tg_send(chat_id, reply)
        q("INSERT INTO dialog_events(user_id,role,text,mi_phase,emotion,relevance) VALUES(%s,'assistant',%s,'engage','neutral',false)",(uid,reply))
        set_last_prompt(uid, reply)
        return {"ok":True}

    # ---- NLU: intents & topics ----
    emo = detect_emotion(text)
    rel, axes, anchors = classify_relevance(text)
    if rel: update_profile(uid, axes, anchors)

    state = app_state_get(uid)
    topic = state.get("topic") or detect_topic(text)
    if topic and not state.get("topic"):
        app_state_patch(uid, {"topic": topic})

    # согласие на юмор
    humor = state.get("humor_opt_in", False)
    yn = wants_humor(text)
    if yn is True:
        humor = True
        app_state_patch(uid, {"humor_opt_in": True})
        await tg_send(chat_id, "Окей, добавлю щепотку юмора там, где уместно 😉")
    elif yn is False:
        humor = False
        app_state_patch(uid, {"humor_opt_in": False})
        await tg_send(chat_id, "Хорошо, оставляю без юмора. Сфокусируемся по-деловому 🌿")

    # «не знаю» → даём варианты и мягко сузим
    if is_unknown(text):
        options = ("Если нащупывать фокус, что ближе сейчас?\n"
                   "1) Уверенность/самооценка\n"
                   "2) Стресс/тревога\n"
                   "3) Отношения\n"
                   "4) Работа/мотивация\n\n"
                   "Можно ответить цифрой или словом.")
        await tg_send(chat_id, options)
        set_last_prompt(uid, options)
        q("INSERT INTO dialog_events(user_id,role,text,mi_phase) VALUES(%s,'assistant',%s,'engage')",(uid,options))
        return {"ok":True}

    # если пользователь попросил «как/что делать» → даём короткую структуру по теме
    if wants_examples_or_plan(text) or state.get("focus_locked"):
        if not topic:
            topic = "уверенность"
            app_state_patch(uid, {"topic": topic})
        plan = {
            "уверенность": "Мини-план по уверенности: 1) один комплимент себе в заметках, 2) маленький шаг с лёгким риском (1 из 10 по шкале), 3) вечером — что получилось и чему научилась.",
            "стресс": "Мини-план по стрессу: 1) 4 цикла дыхания 4-7-8, 2) разгрузка мыслей списком на 3 минуты, 3) микро-движение на 5 минут.",
            "отношения": "Мини-план по отношениям: 1) назвать чувство и потребность, 2) одна «я-фраза», 3) маленький запрос без требований.",
            "работа": "Мини-план по работе: 1) 10-минутный спринт на самую маленькую задачу, 2) убрать один отвлекающий фактор, 3) отметить прогресс.",
            "мотивация": "Мини-план по мотивации: 1) сформулировать «зачем», 2) шаг на 5 минут, 3) поощрение за выполнение."
        }.get(topic, "Давай выберем один маленький шаг, который займёт 5–10 минут, и сделаем его сегодня.")
        if humor: plan += " (и без фанатизма — геройство отменяется, нам нужен «микро-шаг», а не подвиг 😅)"
        await tg_send(chat_id, plan + "\n\nКакой первый шаг возьмёшь?")
        app_state_patch(uid, {"focus_locked": True})
        set_last_prompt(uid, plan)
        q("INSERT INTO dialog_events(user_id,role,text,mi_phase) VALUES(%s,'assistant',%s,'plan')",(uid,plan))
        return {"ok":True}

    # обычный ход: фаза + персонализированный вопрос по теме
    last = q("SELECT mi_phase FROM dialog_events WHERE user_id=%s ORDER BY id DESC LIMIT 1",(uid,))
    last_phase = last[0]["mi_phase"] if last else "engage"
    phase = choose_phase(last_phase, emo, text)

    # Если тема обнаружена — подсказываем фокус фразой по теме
    if topic and phase in ("engage","focus"):
        lead = reflect_emotion(text)
        ask  = {
            "уверенность": "Что именно подтачивает уверенность сильнее всего — мысли, ситуации или люди?",
            "стресс": "Где стресс проявляется заметнее — тело, мысли или поведение?",
            "отношения": "Про кого сейчас больше — про близких, семью, коллег или про тебя саму?",
            "работа": "Что сейчас болит на работе — задачи, люди или правила?",
            "мотивация": "Что делает старт трудным — нет смысла, страшно или скучно?"
        }.get(topic, open_question(phase, comms_style({"ei":0.5,"sn":0.5,"tf":0.5,"jp":0.5})))
        draft = f"{lead}{ask}"
    else:
        draft = personalized_reply(uid, text, phase)

    if humor and phase in ("engage","focus") and "?" in draft:
        draft += " 🙂"

    if not quality_ok(draft) or is_same_prompt(uid, draft):
        draft = vary_prompt("fallback") + " 🌿"

    # лог и отправка
    q("""INSERT INTO dialog_events(user_id,role,text,mi_phase,emotion,relevance,axes)
         VALUES(%s,'user',%s,%s,%s,%s,%s)""",
      (uid, text, phase, emo, rel, json.dumps(axes if rel else {})))
    q("""INSERT INTO dialog_events(user_id,role,text,mi_phase,emotion,relevance)
         VALUES(%s,'assistant',%s,%s,%s,%s)""",
      (uid, draft, phase, emo, rel))

    await tg_send(chat_id, draft)
    set_last_prompt(uid, draft)
    return {"ok":True}

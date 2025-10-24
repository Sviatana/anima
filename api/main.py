import os
import re
import json
from typing import Any, Dict, Optional, List, Tuple

from fastapi import FastAPI, Request, Header
from pydantic import BaseModel
from dotenv import load_dotenv
import httpx
import psycopg2
import psycopg2.extras

load_dotenv()
app = FastAPI(title="ANIMA 2.0")

# ---------- ENV ----------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
DB_URL = os.getenv("DATABASE_URL", "")
REPORTS_TOKEN = os.getenv("REPORTS_TOKEN", "")

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
    """Отправка сообщений в Telegram"""
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

def crisis_detect(t: str) -> bool:
    return bool(CRISIS.search(t))

# ---------- Emotion ----------
def detect_emotion(t: str) -> str:
    tl = t.lower()
    if re.search(r"устал|напряж|тревож|страш|злюсь|злость|раздраж", tl):
        return "tense"
    if re.search(r"спокойн|рад|легко|хорошо|получилось", tl):
        return "calm"
    if re.search(r"не знаю|путаюсь|сомнева", tl):
        return "uncertain"
    return "neutral"

# ---------- MI Phase FSM ----------
def choose_phase(last_phase: str, emotion: str, text: str) -> str:
    tl = text.lower()
    # при напряжении или растерянности — всегда возвращаемся в engage
    if emotion in ("tense", "uncertain"):
        return "engage"
    # явные маркеры
    if re.search(r"\bфокус\b|главн|сосредоточ", tl):
        return "focus"
    if re.search(r"\bпочему\b|\bзачем\b|думаю|хочу понять|кажется", tl):
        return "evoke"
    if re.search(r"готов|сделаю|попробую|начну|планир", tl):
        return "plan"
    # по умолчанию: сохраняем либо мягко уводим к focus после engage
    return "focus" if last_phase == "engage" else last_phase

# ---------- KNO (теплые вопросы) ----------
KNO = [
    ("ei_q1", "Когда ты устаёшь — что помогает быстрее восстановиться: пообщаться с людьми 🌱 или побыть наедине ☁️?"),
    ("sn_q1", "Что тебе ближе: действовать по конкретным шагам и фактам 🧭 или ориентироваться на идею и смысл ✨?"),
    ("tf_q1", "Как ты чаще принимаешь решения: через логику и аргументы 🧠 или через чувства и внутренние ценности 💛?"),
    ("jp_q1", "Когда тебе спокойнее: когда всё чётко спланировано 📋 или когда есть свобода и место для импровизации 🌊?"),
    ("jp_q2", "Если дел много — предпочитаешь составить список заранее 📅 или пробовать и смотреть по ситуации 🎈?"),
    ("ei_q2", "Когда хочешь разобраться в мыслях — поговорить с кем-то 🗣 или записать их для себя ✍️?")
]
KNO_MAP = {"ei_q1":("E","I"), "sn_q1":("S","N"), "tf_q1":("T","F"), "jp_q1":("J","P"), "jp_q2":("J","P"), "ei_q2":("E","I")}

def ensure_user(uid:int, username=None, first_name=None, last_name=None):
    q(
        """INSERT INTO user_profile(user_id,username,first_name,last_name)
           VALUES(%s,%s,%s,%s)
           ON CONFLICT (user_id) DO NOTHING""",
        (uid, username, first_name, last_name)
    )

def app_state_get(uid:int)->Dict[str,Any]:
    r = q("SELECT facts FROM user_profile WHERE user_id=%s", (uid,))
    if not r:
        return {}
    return r[0]["facts"].get("app_state", {}) if r[0]["facts"] else {}

def app_state_set(uid:int, patch:Dict[str,Any]):
    r = q("SELECT facts FROM user_profile WHERE user_id=%s", (uid,))
    facts = r[0]["facts"] if r and r[0]["facts"] else {}
    st = facts.get("app_state", {})
    st.update(patch)
    facts["app_state"] = st
    q("UPDATE user_profile SET facts=%s, updated_at=NOW() WHERE user_id=%s", (json.dumps(facts), uid))

def kno_start(uid:int):
    app_state_set(uid, {"kno_idx": 0, "kno_answers": {}})

def kno_step(uid:int, text:str)->Optional[str]:
    st = app_state_get(uid)
    idx = st.get("kno_idx", 0)
    answers = st.get("kno_answers", {})

    # Нормализуем ответ: цифры 1/2 или ключевые слова
    t = (text or "").strip().lower()

    def pick_by_keywords(question_key:str, t:str)->int:
        # 1 — первый вариант, 2 — второй
        if t in {"1", "первый", "первое", "первая"}:
            return 1
        if t in {"2", "второй", "второе", "вторая"}:
            return 2

        # мягкие эвристики по каждому вопросу
        if question_key.startswith("ei_"):
            if ("наедин" in t) or ("один" in t) or ("тишин" in t):
                return 2
            if ("люд" in t) or ("общат" in t) or ("встреч" in t):
                return 1
        if question_key.startswith("sn_"):
            if ("факт" in t) or ("конкрет" in t) or ("шаг" in t):
                return 1
            if ("смысл" in t) or ("иде" in t) or ("образ" in t):
                return 2
        if question_key.startswith("tf_"):
            if ("логик" in t) or ("рацион" in t) or ("аргумент" in t):
                return 1
            if ("чувств" in t) or ("эмоци" in t) or ("ценност" in t):
                return 2
        if question_key.startswith("jp_"):
            if ("план" in t) or ("распис" in t) or ("контрол" in t):
                return 1
            if ("свобод" in t) or ("импров" in t) or ("спонтан" in t):
                return 2

        # если совсем не похоже — по умолчанию первый вариант
        return 1

    key, _ = KNO[idx]
    choice = pick_by_keywords(key, t)
    answers[key] = choice

    idx += 1
    if idx >= len(KNO):
        # compute axes
        axes = {"E":0,"I":0,"S":0,"N":0,"T":0,"F":0,"J":0,"P":0}
        for k, v in answers.items():
            a, b = KNO_MAP[k]
            axes[a if v == 1 else b] += 1

        def norm(a, b):
            s = a + b
            return ((a / (s or 1)), (b / (s or 1)))

        E, I = norm(axes["E"], axes["I"])
        S, N = norm(axes["S"], axes["N"])
        T, F = norm(axes["T"], axes["F"])
        J, P = norm(axes["J"], axes["P"])

        q(
            """INSERT INTO psycho_profile(user_id,ei,sn,tf,jp,confidence,mbti_type,anchors,state)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (user_id) DO UPDATE
                 SET ei=EXCLUDED.ei,sn=EXCLUDED.sn,tf=EXCLUDED.tf,jp=EXCLUDED.jp,
                     confidence=EXCLUDED.confidence,updated_at=NOW()""",
            (uid, E, N, T, J, 0.4, None, json.dumps([]), None)
        )
        app_state_set(uid, {"kno_done": True, "kno_idx": None, "kno_answers": answers})
        return None
    else:
        app_state_set(uid, {"kno_idx": idx, "kno_answers": answers})
        return KNO[idx][1]

# ---------- Relevance & MBTI update ----------
def classify_relevance(t:str)->Tuple[bool,Dict[str,float],List[Dict[str,Any]]]:
    axes: Dict[str, float] = {}
    anchors: List[Dict[str, Any]] = []
    rel = False
    tl = (t or "").lower()
    if re.search(r"планир|расписан|контролир", tl):
        axes["jp"] = axes.get("jp", 0) + 0.2
        anchors.append({"axis":"jp","quote":"планирование"})
        rel = True
    if re.search(r"спонтан|импровиз", tl):
        axes["jp"] = axes.get("jp", 0) - 0.2
        anchors.append({"axis":"jp","quote":"спонтанность"})
        rel = True
    if re.search(r"встреч|команда|люд(ей|ям)|общаться", tl):
        axes["ei"] = axes.get("ei", 0) + 0.2
        anchors.append({"axis":"ei","quote":"общительность"})
        rel = True
    if re.search(r"тишин|один|наедине", tl):
        axes["ei"] = axes.get("ei", 0) - 0.2
        anchors.append({"axis":"ei","quote":"уединение"})
        rel = True
    if re.search(r"факты|пошагов|конкретн", tl):
        axes["sn"] = axes.get("sn", 0) - 0.15
        anchors.append({"axis":"sn","quote":"факты"})
        rel = True
    if re.search(r"смысл|образ|идея", tl):
        axes["sn"] = axes.get("sn", 0) + 0.15
        anchors.append({"axis":"sn","quote":"смыслы"})
        rel = True
    if re.search(r"логик|рацио|сравн", tl):
        axes["tf"] = axes.get("tf", 0) + 0.15
        anchors.append({"axis":"tf","quote":"анализ"})
        rel = True
    if re.search(r"чувств|гармони|эмоци", tl):
        axes["tf"] = axes.get("tf", 0) - 0.15
        anchors.append({"axis":"tf","quote":"эмпатия"})
        rel = True
    return rel, axes, anchors

def ewma(v:float, delta:float, alpha:float=0.1)->float:
    return max(0.0, min(1.0, v + alpha * delta))

def to_mbti(ei,sn,tf,jp)->str:
    return ("E" if ei>=0.5 else "I") + ("N" if sn>=0.5 else "S") + ("T" if tf>=0.5 else "F") + ("J" if jp>=0.5 else "P")

def update_profile(uid:int, delta:Dict[str,float], anchors:List[Dict[str,Any]]):
    rows = q("SELECT ei,sn,tf,jp,confidence,anchors FROM psycho_profile WHERE user_id=%s", (uid,))
    if not rows:
        ensure_user(uid)
        q("INSERT INTO psycho_profile(user_id) VALUES(%s)", (uid,))
        rows = q("SELECT ei,sn,tf,jp,confidence,anchors FROM psycho_profile WHERE user_id=%s", (uid,))
    p = rows[0]
    ei, sn, tf, jp = p["ei"], p["sn"], p["tf"], p["jp"]
    if "ei" in delta: ei = ewma(ei, delta["ei"])
    if "sn" in delta: sn = ewma(sn, delta["sn"])
    if "tf" in delta: tf = ewma(tf, delta["tf"])
    if "jp" in delta: jp = ewma(jp, delta["jp"])
    conf = min(0.99, (p["confidence"] or 0.0) + (0.02 if delta else 0.0))
    anc = (p["anchors"] or []) + anchors
    mbti = to_mbti(ei, sn, tf, jp) if conf >= 0.4 else None
    q(
        """UPDATE psycho_profile
              SET ei=%s,sn=%s,tf=%s,jp=%s,
                  confidence=%s,mbti_type=%s,anchors=%s,updated_at=NOW()
            WHERE user_id=%s""",
        (ei, sn, tf, jp, conf, mbti, json.dumps(anc[-50:]), uid)
    )

# ---------- Personalization ----------
def comms_style(p:Dict[str,Any])->Dict[str,str]:
    return {
        "tone":   "активный" if p.get("ei",0.5) >= 0.5 else "спокойный",
        "detail": "смыслы"   if p.get("sn",0.5) >= 0.5 else "шаги",
        "mind":   "анализ"   if p.get("tf",0.5) >= 0.5 else "чувства",
        "plan":   "план"     if p.get("jp",0.5) >= 0.5 else "эксперимент"
    }

def reflect_emotion(text:str)->str:
    t = (text or "").lower()
    if re.search(r"устал|напряж|тревож|злюсь|злость|раздраж", t):
        return "Понимаю, как непросто бывает, когда накапливается напряжение 💭. "
    if re.search(r"спокойн|рад|легко|получилось", t):
        return "Классно слышать, что в тебе есть спокойствие и лёгкость 🌿. "
    if re.search(r"не знаю|путаюсь|сомнева", t):
        return "Кажется, сейчас хочется больше ясности и поддержки 💫. "
    return "Я рядом и готова тебя слушать 💬. "

def open_question(phase:str, style:Dict[str,str])->str:
    if phase == "engage":
        return "С чего тебе хотелось бы начать прямо сейчас?"
    if phase == "focus":
        return "На чём тебе важно остановиться в первую очередь?"
    if phase == "evoke":
        return "Какой смысл ты видишь здесь?" if style["detail"] == "смыслы" else "Какие конкретные шаги ты видишь здесь?"
    if phase == "plan":
        return "Какой маленький шаг ты готова запланировать на сегодня?" if style["plan"] == "план" else "Какой лёгкий эксперимент попробуешь сначала?"
    return "Расскажи ещё немного — что за этим стоит?"

def personalized_reply(uid:int, text:str, phase:str)->str:
    pr = q("SELECT ei,sn,tf,jp,mbti_type FROM psycho_profile WHERE user_id=%s", (uid,))
    p = pr[0] if pr else {"ei":0.5,"sn":0.5,"tf":0.5,"jp":0.5}
    st = comms_style(p)
    return f"{reflect_emotion(text)}{open_question(phase, st)}"

# ---------- Quality Gate ----------
def quality_ok(s:str)->bool:
    if STOP.search(s):
        return False
    L = len(s or "")
    if L < 90 or L > 350:
        return False
    if "?" not in s:
        return False
    # мягкая проверка эмпатической лексики
    if not re.search(r"(слышу|вижу|понимаю|рядом|важно|готова|поддержк)", (s or "").lower()):
        return False
    return True

# ---------- API ----------
@app.get("/")
async def root():
    return {"ok": True, "service": "anima"}

@app.post("/webhook/telegram")
async def webhook(update: TelegramUpdate, request: Request):
    if not update.message:
        return {"ok": True}

    msg = update.message
    chat_id = msg["chat"]["id"]
    uid = chat_id
    text = (msg.get("text") or "").strip()
    u = msg.get("from", {})
    ensure_user(uid, u.get("username"), u.get("first_name"), u.get("last_name"))

    # Safety first
    if crisis_detect(text):
        reply = (
            "Я рядом и слышу твою боль. Если нужна поддержка — обратись к близким или в службу помощи. "
            "Что сейчас было бы самым поддерживающим?"
        )
        await tg_send(chat_id, reply)
        q("INSERT INTO dialog_events(user_id,role,text,mi_phase,emotion,relevance) VALUES(%s,'assistant',%s,'support','tense',false)", (uid, reply))
        return {"ok": True}

    if STOP.search(text):
        reply = "Давай оставим чувствительные темы за рамками. О чём тебе важнее поговорить сейчас?"
        await tg_send(chat_id, reply)
        q("INSERT INTO dialog_events(user_id,role,text,mi_phase,emotion,relevance) VALUES(%s,'assistant',%s,'engage','neutral',false)", (uid, reply))
        return {"ok": True}

    # Onboarding KNO
    st = app_state_get(uid)
    if text.lower() in ("/start", "старт", "начать") or not st.get("kno_done"):
        # если индекса ещё нет — это самое первое касание: стартуем и задаём 1-й вопрос
        if st.get("kno_idx") is None:
            kno_start(uid)
            q1 = KNO[0][1]
            greet = (
                "Привет 🌿 Я Анима — твой личный психологический ассистент. "
                "Помогаю лучше понимать себя, находить баланс и опору в повседневных ситуациях. "
                "Чтобы адаптировать поддержку под тебя, задам несколько коротких вопросов. "
                "Отвечай цифрой 1 или 2, можно своими словами 😊\n\n"
            )
            await tg_send(chat_id, f"{greet}{q1}\n\nОтветь 1 или 2, можно словами.")
            q("INSERT INTO dialog_events(user_id,role,text,mi_phase) VALUES(%s,'assistant',%s,'engage')", (uid, q1))
            return {"ok": True}

        # иначе мы уже в анкете — обрабатываем текущий ответ и задаём следующий вопрос
        nxt = kno_step(uid, text)
        if nxt is None:
            prof = q("SELECT ei,sn,tf,jp,confidence FROM psycho_profile WHERE user_id=%s", (uid,))
            prof = prof[0] if prof else {"confidence": 0.4}
            conf = int((prof.get("confidence") or 0) * 100)
            reply = (
                f"Спасибо, я уже немного тебя почувствовала 💫\n"
                f"Пока что моё первое впечатление с уверенностью {conf}%.\n"
                "С каждым разговором я буду понимать тебя лучше и точнее подбирать поддержку.\n"
                "Теперь можем просто поговорить — о настроении, целях, сложностях или вдохновении 🌿"
            )
            await tg_send(chat_id, reply)
            q("INSERT INTO dialog_events(user_id,role,text,mi_phase) VALUES(%s,'assistant',%s,'engage')", (uid, reply))
            return {"ok": True}
        else:
            await tg_send(chat_id, nxt + "\n\nОтветь 1 или 2, можно словами.")
            q("INSERT INTO dialog_events(user_id,role,text,mi_phase) VALUES(%s,'assistant',%s,'engage')", (uid, nxt))
            return {"ok": True}

    # Emotion and relevance during free dialog
    emo = detect_emotion(text)
    rel, axes, anchors = classify_relevance(text)
    if rel:
        update_profile(uid, axes, anchors)

    # MI phase + personalized reply
    last = q("SELECT mi_phase FROM dialog_events WHERE user_id=%s ORDER BY id DESC LIMIT 1", (uid,))
    last_phase = last[0]["mi_phase"] if last else "engage"
    phase = choose_phase(last_phase, emo, text)
    draft = personalized_reply(uid, text, phase)
    if not quality_ok(draft):
        draft = "Слышу тебя 🌿 Расскажи немного больше — что за этим стоит или что сейчас важнее всего для тебя?"

    # Send + log
    await tg_send(chat_id, draft)
    q(
        """INSERT INTO dialog_events(user_id,role,text,mi_phase,emotion,relevance,axes)
           VALUES(%s,'user',%s,%s,%s,%s,%s)""",
        (uid, text, phase, emo, rel, json.dumps(axes if rel else {}))
    )
    q(
        """INSERT INTO dialog_events(user_id,role,text,mi_phase,emotion,relevance)
           VALUES(%s,'assistant',%s,%s,%s,%s)""",
        (uid, draft, phase, emo, rel)
    )
    return {"ok": True}

# ---------- Daily topics ----------
@app.post("/jobs/daily-topics/run-for/{uid}")
async def daily_topics_for(uid: int, payload: Dict[str, Any] = None):
    # простая персонализация по профилю
    p = q("SELECT ei,sn,tf,jp FROM psycho_profile WHERE user_id=%s", (uid,))
    p = p[0] if p else None

    topics: List[Dict[str,str]] = []
    if p and p["jp"] is not None and p["jp"] >= 0.5:
        topics.append({"title":"Один маленький шаг на сегодня", "why":"тебе помогает план и порядок"})
    else:
        topics.append({"title":"Лёгкий эксперимент на сегодня", "why":"тебе помогает гибкость и проба"})
    if p and p["sn"] is not None and p["sn"] >= 0.5:
        topics.append({"title":"Какие конкретные шаги приблизят цель", "why":"конкретика снижает напряжение"})
    else:
        topics.append({"title":"Какой смысл ты видишь сейчас", "why":"смысл даёт энергию двигаться"})
    topics.append({"title":"Что помогает тебе восстанавливаться", "why":"поддержка ресурса важна ежедневно"})

    q(
        """INSERT INTO daily_topics(user_id, topics)
           VALUES(%s,%s)
           ON CONFLICT (user_id) DO NOTHING""",
        (uid, json.dumps(topics))
    )
    return {"user_id": uid, "topics": topics}

# ---------- Reports ----------
def auth_reports(x_token: str) -> bool:
    return (not REPORTS_TOKEN) or (x_token == REPORTS_TOKEN)

@app.get("/reports/summary")
async def reports_summary(x_token: str = Header(default="")):
    if not auth_reports(x_token):
        return {"error": "unauthorized"}

    kpi = q(
        """
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
        """
    )
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
        return {"error": "unauthorized"}
    prof = q("SELECT * FROM psycho_profile WHERE user_id=%s", (uid,))
    last_events = q(
        """
        SELECT role, text, emotion, mi_phase, relevance, created_at
        FROM dialog_events
        WHERE user_id=%s
        ORDER BY id DESC LIMIT 30
        """,
        (uid,)
    )
    quality = q(
        """
        SELECT day, avg_quality, safety_rate, answers_total
        FROM v_quality_score
        WHERE user_id=%s
        ORDER BY day DESC LIMIT 14
        """,
        (uid,)
    )
    return {
        "profile": prof[0] if prof else {},
        "last_events": last_events or [],
        "quality_14d": quality or []
    }

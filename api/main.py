# api/main.py
import os
import re
import json
import traceback
from typing import Any, Dict, Optional, List, Tuple

from fastapi import FastAPI, Request, Header
from pydantic import BaseModel
from dotenv import load_dotenv

import httpx
import psycopg2
import psycopg2.extras

# -----------------------------------------------------------------------------
# Init & config
# -----------------------------------------------------------------------------
load_dotenv()
app = FastAPI(title="ANIMA 2.0")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
DB_URL = os.getenv("DATABASE_URL", "")
REPORTS_TOKEN = os.getenv("REPORTS_TOKEN", "")

# -----------------------------------------------------------------------------
# Helpers: DB
# -----------------------------------------------------------------------------
def db():
    if not DB_URL:
        raise RuntimeError("DATABASE_URL is empty")
    return psycopg2.connect(DB_URL)

def q(query: str, params: Tuple = (), fetch: bool = True):
    """Single-shot query with RealDictCursor. Returns list[dict] or None."""
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

def safe_ddl(sql: str):
    """Run DDL/DDL-like statement; ignore if already exists or conflicts."""
    try:
        q(sql, fetch=False)
    except Exception:
        # keep logs but don't crash on idempotent DDL
        print("[DDL WARN]", sql[:120], "…")
        traceback.print_exc()

# -----------------------------------------------------------------------------
# Schema auto-migration (idempotent)
# -----------------------------------------------------------------------------
def ensure_schema():
    # Core tables
    safe_ddl("""
    CREATE TABLE IF NOT EXISTS user_profile (
      user_id BIGINT PRIMARY KEY,
      username   TEXT,
      first_name TEXT,
      last_name  TEXT,
      locale     TEXT,
      facts      JSONB DEFAULT '{}'::jsonb,
      created_at TIMESTAMP DEFAULT NOW(),
      updated_at TIMESTAMP DEFAULT NOW()
    );
    """)

    safe_ddl("""
    CREATE TABLE IF NOT EXISTS psycho_profile (
      user_id BIGINT PRIMARY KEY,
      ei FLOAT DEFAULT 0.5,
      sn FLOAT DEFAULT 0.5,
      tf FLOAT DEFAULT 0.5,
      jp FLOAT DEFAULT 0.5,
      confidence FLOAT DEFAULT 0.3,
      mbti_type  TEXT,
      anchors    JSONB DEFAULT '[]'::jsonb,
      state      TEXT,
      updated_at TIMESTAMP DEFAULT NOW(),
      CONSTRAINT psycho_profile_user_fk
        FOREIGN KEY (user_id) REFERENCES user_profile(user_id) ON DELETE CASCADE
    );
    """)

    # Guarantee unique/PK for ON CONFLICT usage even if table came from older schema
    safe_ddl("""CREATE UNIQUE INDEX IF NOT EXISTS ux_psycho_profile_user ON psycho_profile(user_id);""")

    safe_ddl("""
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
      created_at TIMESTAMP DEFAULT NOW()
    );
    """)

    # daily_topics как отдельный справочник (на пользователя один актуальный набор)
    safe_ddl("""
    CREATE TABLE IF NOT EXISTS daily_topics (
      user_id BIGINT PRIMARY KEY,
      topics  JSONB NOT NULL,
      created_at TIMESTAMPTZ DEFAULT NOW(),
      CONSTRAINT daily_topics_user_fk
        FOREIGN KEY (user_id) REFERENCES user_profile(user_id) ON DELETE CASCADE
    );
    """)

    safe_ddl("""
    CREATE TABLE IF NOT EXISTS reports (
      id BIGSERIAL PRIMARY KEY,
      user_id BIGINT REFERENCES user_profile(user_id) ON DELETE CASCADE,
      kind TEXT,        -- summary | user_snapshot
      content JSONB,
      created_at TIMESTAMP DEFAULT NOW()
    );
    """)

    # Indexes
    safe_ddl("CREATE INDEX IF NOT EXISTS idx_dialog_user_created ON dialog_events(user_id, created_at DESC);")
    safe_ddl("CREATE INDEX IF NOT EXISTS idx_dialog_role ON dialog_events(role);")
    safe_ddl("CREATE INDEX IF NOT EXISTS idx_dialog_phase ON dialog_events(mi_phase);")
    safe_ddl("CREATE INDEX IF NOT EXISTS idx_dialog_emotion ON dialog_events(emotion);")
    safe_ddl("CREATE INDEX IF NOT EXISTS idx_psycho_conf ON psycho_profile(confidence DESC);")

    # Views (best-effort)
    safe_ddl("DROP VIEW IF EXISTS v_message_lengths;")
    safe_ddl("""
    CREATE VIEW v_message_lengths AS
    SELECT id, user_id, role, length(coalesce(text,'')) AS len, created_at
    FROM dialog_events;
    """)

    safe_ddl("DROP VIEW IF EXISTS v_quality_flags;")
    safe_ddl("""
    CREATE VIEW v_quality_flags AS
    SELECT
      e.id,
      e.user_id,
      e.role,
      e.text,
      e.mi_phase,
      e.emotion,
      e.created_at,
      (position('?' in coalesce(e.text,'')) > 0) AS has_question,
      (length(coalesce(e.text,'')) BETWEEN 90 AND 350) AS in_target_len,
      (e.text ~* '(слышу|вижу|понимаю|рядом|важно)') AS has_empathy,
      (e.text ~* '(политик|религ|насили|медицинск|вакцин|диагноз|лекарств|суицид)') AS has_banned
    FROM dialog_events e
    WHERE e.role = 'assistant';
    """)

    safe_ddl("DROP VIEW IF EXISTS v_quality_score;")
    safe_ddl("""
    CREATE VIEW v_quality_score AS
    SELECT
      user_id,
      date_trunc('day', created_at) AS day,
      avg( (has_question::int + in_target_len::int + has_empathy::int) / 3.0 ) AS avg_quality,
      sum((NOT has_banned)::int)::float / NULLIF(count(*),0) AS safety_rate,
      count(*) AS answers_total
    FROM v_quality_flags
    GROUP BY user_id, date_trunc('day', created_at);
    """)

    safe_ddl("DROP VIEW IF EXISTS v_phase_dist;")
    safe_ddl("""
    CREATE VIEW v_phase_dist AS
    SELECT date_trunc('day', created_at) AS day, mi_phase, count(*) AS cnt
    FROM dialog_events
    WHERE role='assistant'
    GROUP BY 1,2;
    """)

    safe_ddl("DROP VIEW IF EXISTS v_len_daily;")
    safe_ddl("""
    CREATE VIEW v_len_daily AS
    SELECT date_trunc('day', created_at) AS day, avg(len) AS avg_len
    FROM v_message_lengths
    WHERE role='assistant'
    GROUP BY 1;
    """)

    safe_ddl("DROP VIEW IF EXISTS v_confidence_hist;")
    safe_ddl("""
    CREATE VIEW v_confidence_hist AS
    SELECT
      width_bucket(confidence, 0, 1, 10) AS bucket,
      count(*) AS users
    FROM psycho_profile
    GROUP BY 1
    ORDER BY 1;
    """)

    safe_ddl("DROP VIEW IF EXISTS v_retention_7d;")
    safe_ddl("""
    CREATE VIEW v_retention_7d AS
    WITH first_seen AS (
      SELECT user_id, min(created_at)::date AS first_day
      FROM dialog_events
      GROUP BY user_id
    ),
    active_last_7 AS (
      SELECT DISTINCT user_id
      FROM dialog_events
      WHERE created_at >= NOW() - INTERVAL '7 days'
    )
    SELECT
      count(a.user_id)::float / NULLIF((SELECT count(*) FROM first_seen),0) AS active_share_7d
    FROM active_last_7 a;
    """)

ensure_schema()
print("✅ DB schema ensured")

# -----------------------------------------------------------------------------
# Telegram helpers
# -----------------------------------------------------------------------------
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

# -----------------------------------------------------------------------------
# Safety & heuristics
# -----------------------------------------------------------------------------
STOP = re.compile(r"(политик|религ|насили|медицинск|вакцин|диагноз|лекарств|суицид)", re.IGNORECASE)
CRISIS = re.compile(r"(не хочу жить|самоповрежд|отчаяни|суицид|покончи|боль невыносима)", re.IGNORECASE)

def crisis_detect(t: str) -> bool:
    return bool(CRISIS.search(t or ""))

def detect_emotion(t: str) -> str:
    tl = (t or "").lower()
    if re.search(r"устал|напряж|тревож|страш|злюсь|злость|раздраж", tl): return "tense"
    if re.search(r"спокойн|рад|легко|хорошо", tl): return "calm"
    if re.search(r"не знаю|путаюсь|сомнева", tl): return "uncertain"
    return "neutral"

def choose_phase(last_phase: str, emotion: str, text: str) -> str:
    tl = (text or "").lower()
    if emotion in ("tense", "uncertain"):
        return "engage"
    if re.search(r"\bфокус\b|главн|сосредоточ", tl): return "focus"
    if re.search(r"\bпочему\b|\bзачем\b|думаю|хочу понять|кажется", tl): return "evoke"
    if re.search(r"готов|сделаю|попробую|начну|планир", tl): return "plan"
    return "focus" if last_phase == "engage" else last_phase

# -----------------------------------------------------------------------------
# KNO (короткая базовая анкета)
# -----------------------------------------------------------------------------
KNO = [
    ("ei_q1", "Когда ты устаёшь — что помогает быстрее восстановиться: пообщаться с людьми 🌿 или побыть наедине ☁️?"),
    ("sn_q1", "Что тебе ближе: действовать по конкретным шагам и фактам 🔎 или ориентироваться на идею и смысл ✨?"),
    ("tf_q1", "Как ты чаще принимаешь решения: через логику и аргументы 🧠 или через чувства и внутренние ценности 💛?"),
    ("jp_q1", "Когда тебе спокойнее: когда всё чётко спланировано 📋 или когда есть свобода и импровизация 🎨?"),
    ("jp_q2", "Когда много задач: список заранее ✅ или пробовать и смотреть по ситуации 🧭?"),
    ("ei_q2", "Когда нужно разобраться: поговорить с кем-то 🗣 или записать мысли для себя ✍️?")
]
KNO_MAP = {"ei_q1":("E","I"), "sn_q1":("S","N"), "tf_q1":("T","F"), "jp_q1":("J","P"), "jp_q2":("J","P"), "ei_q2":("E","I")}

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
    q("UPDATE user_profile SET facts=%s, updated_at=NOW() WHERE user_id=%s",(json.dumps(facts),uid), fetch=False)

def kno_start(uid:int):
    app_state_set(uid, {"kno_idx":0, "kno_answers":{}, "kno_done":False})

def kno_step(uid:int, text:str)->Optional[str]:
    st = app_state_get(uid)
    idx = st.get("kno_idx",0)
    answers = st.get("kno_answers",{})

    # Нормализация 1/2/слова
    t = (text or "").strip().lower()

    def pick_by_keywords(question_key:str, t:str)->int:
        if t in {"1","первый","первое","первая"}:
            return 1
        if t in {"2","второй","второе","вторая"}:
            return 2
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

    key,_ = KNO[idx]
    choice = pick_by_keywords(key, t)
    answers[key] = choice

    idx += 1
    if idx >= len(KNO):
        # Вычисляем оси (E/I, S/N, T/F, J/P)
        axes = {"E":0,"I":0,"S":0,"N":0,"T":0,"F":0,"J":0,"P":0}
        for k,v in answers.items():
            a,b = KNO_MAP[k]
            axes[a if v==1 else b]+=1

        def norm(a,b):
            s = a+b
            return ((a/(s or 1)), (b/(s or 1)))

        E,I = norm(axes["E"],axes["I"])
        S,N = norm(axes["S"],axes["N"])
        T,F = norm(axes["T"],axes["F"])
        J,P = norm(axes["J"],axes["P"])

        # upsert psycho_profile (user_id unique ensured by migration)
        q("""
        INSERT INTO psycho_profile(user_id,ei,sn,tf,jp,confidence,mbti_type,anchors,state)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (user_id) DO UPDATE
          SET ei=EXCLUDED.ei,
              sn=EXCLUDED.sn,
              tf=EXCLUDED.tf,
              jp=EXCLUDED.jp,
              confidence=EXCLUDED.confidence,
              updated_at=NOW();
        """, (uid,E,N,T,J,0.4,None,json.dumps([]),None), fetch=False)

        app_state_set(uid, {"kno_done":True,"kno_idx":None,"kno_answers":answers})
        return None
    else:
        app_state_set(uid, {"kno_idx":idx,"kno_answers":answers})
        return KNO[idx][1]

# -----------------------------------------------------------------------------
# Lightweight relevance & MBTI update during chat
# -----------------------------------------------------------------------------
def classify_relevance(t:str)->Tuple[bool,Dict[str,float],List[Dict[str,Any]]]:
    axes, anchors, rel = {}, [], False
    tl = (t or "").lower()
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
        q("INSERT INTO psycho_profile(user_id) VALUES(%s) ON CONFLICT DO NOTHING",(uid,), fetch=False)
        rows = q("SELECT ei,sn,tf,jp,confidence,anchors FROM psycho_profile WHERE user_id=%s",(uid,))
    p = rows[0]
    ei,sn,tf,jp = p["ei"],p["sn"],p["tf"],p["jp"]
    if "ei" in delta: ei = ewma(ei, delta["ei"])
    if "sn" in delta: sn = ewma(sn, delta["sn"])
    if "tf" in delta: tf = ewma(tf, delta["tf"])
    if "jp" in delta: jp = ewma(jp, delta["jp"])
    conf = min(0.99, (p["confidence"] or 0.3) + (0.02 if delta else 0.0))
    anc = (p["anchors"] or []) + anchors
    mbti = to_mbti(ei,sn,tf,jp) if conf>=0.4 else None
    q("""UPDATE psycho_profile SET ei=%s,sn=%s,tf=%s,jp=%s,
         confidence=%s,mbti_type=%s,anchors=%s,updated_at=NOW()
         WHERE user_id=%s""",(ei,sn,tf,jp,conf,mbti,json.dumps(anc[-50:]),uid), fetch=False)

# -----------------------------------------------------------------------------
# Personalization & replies
# -----------------------------------------------------------------------------
def comms_style(p:Dict[str,Any])->Dict[str,str]:
    return {
        "tone":   "активный" if (p.get("ei") or 0.5) >= 0.5 else "спокойный",
        "detail": "смыслы"   if (p.get("sn") or 0.5) >= 0.5 else "шаги",
        "mind":   "анализ"   if (p.get("tf") or 0.5) >= 0.5 else "чувства",
        "plan":   "план"     if (p.get("jp") or 0.5) >= 0.5 else "эксперимент"
    }

def reflect_emotion(text:str)->str:
    t=(text or "").lower()
    if re.search(r"устал|напряж|тревож|злюсь|злость|раздраж",t): return "Слышу напряжение и заботу о результате. "
    if re.search(r"спокойн|рад|легко|получилось",t): return "Чувствую спокойствие и лёгкость. "
    if re.search(r"не знаю|путаюсь|сомнева",t): return "Вижу, что хочется ясности. "
    return "Я рядом и слышу тебя. "

def open_question(phase:str, style:Dict[str,str])->str:
    if phase=="engage":
        return "Что сейчас для тебя самое важное?"
    if phase=="focus":
        return "На чём тебе хочется остановиться в первую очередь?"
    if phase=="evoke":
        return "Какой смысл ты видишь здесь?" if style["detail"]=="смыслы" else "Какие конкретные шаги ты видишь здесь?"
    if phase=="plan":
        return "Какой маленький шаг запланируем на сегодня?" if style["plan"]=="план" else "С какого лёгкого эксперимента начнём?"
    return "Расскажи немного больше?"

def personalized_reply(uid:int, text:str, phase:str)->str:
    pr = q("SELECT ei,sn,tf,jp,mbti_type FROM psycho_profile WHERE user_id=%s",(uid,))
    p = pr[0] if pr else {"ei":0.5,"sn":0.5,"tf":0.5,"jp":0.5}
    st = comms_style(p)
    base = f"{reflect_emotion(text)}{open_question(phase, st)}"
    # Мягкое расширение, чтобы не быть односложным
    if phase in ("engage","focus"):
        base += " Можешь описать своими словами — я здесь, чтобы поддержать."
    return base

def quality_ok(s:str)->bool:
    if STOP.search(s): return False
    L = len(s or "")
    if L < 90 or L > 350: return False
    if "?" not in (s or ""): return False
    if not re.search(r"(слышу|вижу|понимаю|рядом|важно)", (s or "").lower()):
        return False
    return True

# -----------------------------------------------------------------------------
# API
# -----------------------------------------------------------------------------
@app.get("/")
async def root():
    return {"ok":True,"service":"anima"}

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

        # CRISIS first
        if crisis_detect(text):
            reply = ("Я рядом и слышу твою боль. Если нужна срочная поддержка — обратись к близким "
                     "или в службу помощи своего города. Что сейчас было бы самым поддерживающим?")
            await tg_send(chat_id, reply)
            q("INSERT INTO dialog_events(user_id,role,text,mi_phase,emotion,relevance) VALUES(%s,'assistant',%s,'support','tense',false)",
              (uid,reply), fetch=False)
            return {"ok":True}

        # Banned topics
        if STOP.search(text):
            reply = "Давай оставим чувствительные темы за рамками. О чём тебе важнее поговорить сейчас?"
            await tg_send(chat_id, reply)
            q("INSERT INTO dialog_events(user_id,role,text,mi_phase,emotion,relevance) VALUES(%s,'assistant',%s,'engage','neutral',false)",
              (uid,reply), fetch=False)
            return {"ok":True}

        # Onboarding /start or first touch (consent + what I can do)
        st = app_state_get(uid)
        if text.lower() in ("/start","старт","начать") or not st.get("kno_done"):
            if st.get("kno_idx") is None:
                kno_start(uid)
                intro = (
                    "Привет! Я Анима — дружелюбный психологический помощник. "
                    "Я слушаю внимательно, помогаю найти фокус и подобрать аккуратные шаги. "
                    "Часть данных я использую, чтобы подстраивать стиль общения и составлять мягкий "
                    "профиль — только для диалога с тобой, без маркетинговых рассылок. "
                    "Если что-то не хочется рассказывать — просто скажи 💛"
                )
                await tg_send(chat_id, intro)
                q1 = KNO[0][1]
                go = "Поехали? Отвечай цифрой 1 или 2, можно своими словами 😊"
                await tg_send(chat_id, go + "\n\n" + q1 + "\n\nОтветь 1 или 2, можно словами.")
                q("INSERT INTO dialog_events(user_id,role,text,mi_phase) VALUES(%s,'assistant',%s,'engage')",
                  (uid,intro), fetch=False)
                q("INSERT INTO dialog_events(user_id,role,text,mi_phase) VALUES(%s,'assistant',%s,'engage')",
                  (uid,q1), fetch=False)
                return {"ok": True}

            nxt = kno_step(uid, text)
            if nxt is None:
                prof = q("SELECT ei,sn,tf,jp,confidence FROM psycho_profile WHERE user_id=%s",(uid,))
                conf = int(((prof[0]["confidence"] or 0)*100) if prof else 40)
                reply = (
                    "Спасибо — у меня появилось первое впечатление о твоём стиле. "
                    f"Уверенность {conf}% и будет расти по мере общения. "
                    "Можем перейти к свободному диалогу — расскажи, что сейчас важнее всего?"
                )
                await tg_send(chat_id, reply)
                q("INSERT INTO dialog_events(user_id,role,text,mi_phase) VALUES(%s,'assistant',%s,'engage')",
                  (uid,reply), fetch=False)
                return {"ok": True}
            else:
                await tg_send(chat_id, nxt + "\n\nОтветь 1 или 2, можно словами.")
                q("INSERT INTO dialog_events(user_id,role,text,mi_phase) VALUES(%s,'assistant',%s,'engage')",
                  (uid,nxt), fetch=False)
                return {"ok": True}

        # Free dialog
        emo = detect_emotion(text)
        rel, axes, anchors = classify_relevance(text)
        if rel:
            update_profile(uid, axes, anchors)

        last = q("SELECT mi_phase FROM dialog_events WHERE user_id=%s ORDER BY id DESC LIMIT 1",(uid,))
        last_phase = last[0]["mi_phase"] if last else "engage"
        phase = choose_phase(last_phase, emo, text)
        draft = personalized_reply(uid, text, phase)
        if not quality_ok(draft):
            draft = "Слышу тебя. Что здесь для тебя главное? Расскажи так, как удобно — я рядом."

        await tg_send(chat_id, draft)

        # Log user + assistant
        q("""INSERT INTO dialog_events(user_id,role,text,mi_phase,emotion,relevance,axes)
             VALUES(%s,'user',%s,%s,%s,%s,%s)""",
          (uid, text, phase, emo, rel, json.dumps(axes if rel else {})), fetch=False)

        q("""INSERT INTO dialog_events(user_id,role,text,mi_phase,emotion,relevance)
             VALUES(%s,'assistant',%s,%s,%s,%s)""",
          (uid, draft, phase, emo, rel), fetch=False)
        return {"ok":True}

    except Exception as e:
        # fail-safe: never crash the webhook silently
        print("ERROR in webhook:", e)
        traceback.print_exc()
        try:
            if update and update.message:
                chat_id = update.message["chat"]["id"]
                await tg_send(chat_id, "Кажется, я споткнулась о техническую мелочь. Уже поправляю — можно повторить последнюю мысль?")
        except Exception:
            pass
        return {"ok":False}

# -----------------------------------------------------------------------------
# Jobs & Reports
# -----------------------------------------------------------------------------
@app.post("/jobs/daily-topics/run-for/{uid}")
async def daily_topics_for(uid: int, payload: Dict[str, Any] = None):
    p = q("SELECT ei,sn,tf,jp FROM psycho_profile WHERE user_id=%s",(uid,))
    p = p[0] if p else None
    topics: List[Dict[str,str]] = []
    if p and p["jp"] >= 0.5:
        topics.append({"title":"Один маленький шаг на сегодня","why":"тебе помогает план и порядок"})
    else:
        topics.append({"title":"Лёгкий эксперимент на сегодня","why":"тебе помогает гибкость и проба"})

    if p and p["sn"] >= 0.5:
        topics.append({"title":"Какие конкретные шаги приблизят цель","why":"конкретика снижает напряжение"})
    else:
        topics.append({"title":"Какой смысл ты видишь сейчас","why":"смысл даёт энергию двигаться"})

    topics.append({"title":"Что помогает тебе восстанавливаться","why":"поддержка ресурса важна ежедневно"})

    q("""INSERT INTO daily_topics(user_id, topics)
         VALUES(%s,%s)
         ON CONFLICT (user_id) DO UPDATE SET topics=EXCLUDED.topics, created_at=NOW()""",
      (uid, json.dumps(topics)), fetch=False)
    return {"user_id": uid, "topics": topics}

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

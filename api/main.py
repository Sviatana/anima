import os, re, json
from typing import Any, Dict, Optional, List, Tuple
from datetime import datetime, date, timedelta
from fastapi import FastAPI, Request, Header
from pydantic import BaseModel
from dotenv import load_dotenv
import httpx
import psycopg2, psycopg2.extras

load_dotenv()
app = FastAPI(title="ANIMA 2.0")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
DB_URL         = os.getenv("DATABASE_URL", "")
REPORTS_TOKEN  = os.getenv("REPORTS_TOKEN", "")

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

def ensure_schema():
    # Безопасная инициализация таблицы для домашки
    q("""
    CREATE TABLE IF NOT EXISTS homework_tasks (
      id BIGSERIAL PRIMARY KEY,
      user_id BIGINT NOT NULL,
      text TEXT NOT NULL,
      due_date DATE NOT NULL,
      status TEXT NOT NULL DEFAULT 'open',         -- open|done|deleted
      last_reminded_at TIMESTAMPTZ,               -- когда в последний раз слали напоминание
      created_at TIMESTAMPTZ DEFAULT NOW()
    )""")

@app.on_event("startup")
def _startup():
    ensure_schema()

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
STOP   = re.compile(r"(политик|религ|насили|медицинск|вакцин|диагноз|лекарств|суицид)", re.IGNORECASE)
CRISIS = re.compile(r"(не хочу жить|самоповрежд|отчаяни|суицид|покончи|боль невыносима)", re.IGNORECASE)

def crisis_detect(t: str) -> bool:
    return bool(CRISIS.search(t))

# ---------- Emotion ----------
def detect_emotion(t: str) -> str:
    tl = t.lower()
    if re.search(r"устал|напряж|тревож|страш|злюсь|злость|раздраж|тяжело|грустн|паник", tl): return "tense"
    if re.search(r"спокойн|рад|легко|хорошо|получилось|ясно", tl):                     return "calm"
    if re.search(r"не знаю|путаюсь|сомнева|непонят|растерян", tl):                     return "uncertain"
    return "neutral"

# ---------- MI Phase FSM ----------
def choose_phase(last_phase: str, emotion: str, text: str) -> str:
    tl = text.lower()
    if emotion in ("tense", "uncertain"):
        return "engage"
    if re.search(r"\bфокус\b|главн|сосредоточ", tl):  return "focus"
    if re.search(r"\bпочему\b|\bзачем\b|думаю|хочу понять|кажется", tl): return "evoke"
    if re.search(r"готов|сделаю|попробую|начну|планир|завтра|сегодня|к \d{1,2}\.\d{1,2}", tl): return "plan"
    return "focus" if last_phase == "engage" else last_phase

# ---------- КНО (короткая типология) ----------
KNO = [
    ("ei_q1", "Когда ты устаёшь — что помогает быстрее восстанавливаться: пообщаться с людьми 🌱 или побыть наедине 🌤?"),
    ("sn_q1", "Что тебе ближе: действовать по конкретным шагам и фактам 🎯 или ориентироваться на идею и смысл ✨?"),
    ("tf_q1", "Как ты чаще принимаешь решения: через логику и аргументы 🧠 или через чувства и внутренние ценности 💛?"),
    ("jp_q1", "Когда тебе спокойнее: когда всё чётко спланировано 📋 или когда есть свобода и импровизация 🌊?"),
    ("jp_q2", "Когда много задач: список заранее или пробовать и смотреть по ситуации?"),
    ("ei_q2", "Когда нужно разобраться: поговорить с кем-то или записать мысли для себя?")
]
KNO_MAP = {"ei_q1":("E","I"), "sn_q1":("S","N"), "tf_q1":("T","F"), "jp_q1":("J","P"), "jp_q2":("J","P"), "ei_q2":("E","I")}

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
    app_state_set(uid, {"kno_idx":0, "kno_answers":{}})

def kno_step(uid:int, text:str)->Optional[str]:
    st = app_state_get(uid)
    idx = st.get("kno_idx",0)
    answers = st.get("kno_answers",{})

    t = text.strip().lower()

    def pick_by_keywords(question_key:str, t:str)->int:
        if t in {"1","первый","первое","первая"}: return 1
        if t in {"2","второй","второе","вторая"}: return 2
        if question_key.startswith("ei_"):
            if "наедин" in t or "один" in t or "тишин" in t: return 2
            if "люд"   in t or "общат" in t or "встреч" in t: return 1
        if question_key.startswith("sn_"):
            if "факт" in t or "конкрет" in t or "шаг"  in t:  return 1
            if "смысл" in t or "иде" in t or "образ" in t:    return 2
        if question_key.startswith("tf_"):
            if "логик" in t or "рацион" in t or "аргумент" in t: return 1
            if "чувств" in t or "эмоци"  in t or "ценност" in t:  return 2
        if question_key.startswith("jp_"):
            if "план"  in t or "распис" in t or "контрол" in t:   return 1
            if "свобод" in t or "импров" in t or "спонтан" in t:  return 2
        return 1

    key,_ = KNO[idx]
    choice = pick_by_keywords(key, t)
    answers[key] = choice

    idx += 1
    if idx >= len(KNO):
        axes = {"E":0,"I":0,"S":0,"N":0,"T":0,"F":0,"J":0,"P":0}
        for k,v in answers.items():
            a,b = KNO_MAP[k]; axes[a if v==1 else b]+=1
        def norm(a,b): s=a+b; return ((a/(s or 1)), (b/(s or 1)))
        E,I = norm(axes["E"],axes["I"]); S,N = norm(axes["S"],axes["N"])
        T,F = norm(axes["T"],axes["F"]); J,P = norm(axes["J"],axes["P"])
        q("""INSERT INTO psycho_profile(user_id,ei,sn,tf,jp,confidence,mbti_type,anchors,state)
             VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
             ON CONFLICT (user_id) DO UPDATE SET ei=EXCLUDED.ei,sn=EXCLUDED.sn,tf=EXCLUDED.tf,
               jp=EXCLUDED.jp,confidence=EXCLUDED.confidence,updated_at=NOW()""",
          (uid,E,N,T,J,0.4,None,json.dumps([]),None))
        app_state_set(uid, {"kno_done":True,"kno_idx":None,"kno_answers":answers})
        return None
    else:
        app_state_set(uid, {"kno_idx":idx,"kno_answers":answers})
        return KNO[idx][1]

# ---------- Обновление профиля (MBTI) ----------
def classify_relevance(t:str)->Tuple[bool,Dict[str,float],List[Dict[str,Any]]]:
    axes, anchors, rel = {}, [], False
    tl = t.lower()
    if re.search(r"планир|расписан|контролир", tl): axes["jp"]=axes.get("jp",0)+0.2; anchors.append({"axis":"jp","quote":"планирование"}); rel=True
    if re.search(r"спонтан|импровиз", tl):       axes["jp"]=axes.get("jp",0)-0.2; anchors.append({"axis":"jp","quote":"спонтанность"}); rel=True
    if re.search(r"встреч|команда|люд(ей|ям)|общать", tl): axes["ei"]=axes.get("ei",0)+0.2; anchors.append({"axis":"ei","quote":"общительность"}); rel=True
    if re.search(r"тишин|один|наедине", tl):     axes["ei"]=axes.get("ei",0)-0.2; anchors.append({"axis":"ei","quote":"уединение"}); rel=True
    if re.search(r"факты|пошагов|конкретн", tl): axes["sn"]=axes.get("sn",0)-0.15; anchors.append({"axis":"sn","quote":"факты"}); rel=True
    if re.search(r"смысл|образ|идея", tl):       axes["sn"]=axes.get("sn",0)+0.15; anchors.append({"axis":"sn","quote":"смыслы"}); rel=True
    if re.search(r"логик|рацио|сравн", tl):      axes["tf"]=axes.get("tf",0)+0.15; anchors.append({"axis":"tf","quote":"анализ"}); rel=True
    if re.search(r"чувств|гармони|эмоци", tl):   axes["tf"]=axes.get("tf",0)-0.15; anchors.append({"axis":"tf","quote":"эмпатия"}); rel=True
    return rel, axes, anchors

def ewma(v:float, delta:float, alpha:float=0.1)->float:
    return max(0.0, min(1.0, v + alpha * delta))

def to_mbti(ei,sn,tf,jp)->str:
    return ("E" if ei>=0.5 else "I")+("N" if sn>=0.5 else "S")+("T" if tf>=0.5 else "F")+("J" if jp>=0.5 else "P")

def update_profile(uid:int, delta:Dict[str,float], anchors:List[Dict[str,Any]]):
    rows = q("SELECT ei,sn,tf,jp,confidence,anchors FROM psycho_profile WHERE user_id=%s",(uid,))
    if not rows:
        ensure_user(uid); q("INSERT INTO psycho_profile(user_id) VALUES(%s)",(uid,))
        rows = q("SELECT ei,sn,tf,jp,confidence,anchors FROM psycho_profile WHERE user_id=%s",(uid,))
    p = rows[0]
    ei,sn,tf,jp = p["ei"],p["sn"],p["tf"],p["jp"]
    if "ei" in delta: ei = ewma(ei, delta["ei"])
    if "sn" in delta: sn = ewma(sn, delta["sn"])
    if "tf" in delta: tf = ewma(tf, delta["tf"])
    if "jp" in delta: jp = ewma(jp, delta["jp"])
    conf = min(0.99, p["confidence"] + (0.02 if delta else 0.0))
    anc = (p["anchors"] or []) + anchors
    mbti = to_mbti(ei,sn,tf,jp) if conf>=0.4 else None
    q("""UPDATE psycho_profile SET ei=%s,sn=%s,tf=%s,jp=%s,
         confidence=%s,mbti_type=%s,anchors=%s,updated_at=NOW()
         WHERE user_id=%s""",(ei,sn,tf,jp,conf,mbti,json.dumps(anc[-50:]),uid))

# ---------- Персонализация тона ----------
def comms_style(p:Dict[str,Any])->Dict[str,str]:
    return {
        "tone":   "активный" if p.get("ei",0.5)>=0.5 else "спокойный",
        "detail": "смыслы"   if p.get("sn",0.5)>=0.5 else "шаги",
        "mind":   "анализ"   if p.get("tf",0.5)>=0.5 else "чувства",
        "plan":   "план"     if p.get("jp",0.5)>=0.5 else "эксперимент"
    }

def reflect_emotion(text:str)->str:
    t=text.lower()
    if re.search(r"устал|напряж|тревож|злюсь|злость|раздраж|тяжело|грустн|паник",t): return "Слышу напряжение и заботу о результате. "
    if re.search(r"спокойн|рад|легко|получилось|хорошо|ясно",t):                  return "Чувствую спокойствие и лёгкость. "
    if re.search(r"не знаю|путаюсь|сомнева|непонят|растерян",t):                  return "Вижу, что хочется ясности. "
    return "Я рядом и слышу тебя. "

def open_question(phase:str, style:Dict[str,str])->str:
    if phase=="engage": return "Что сейчас для тебя самое важное?"
    if phase=="focus":  return "На чём тебе хочется остановиться в первую очередь?"
    if phase=="evoke":
        return "Какой смысл ты видишь здесь?" if style["detail"]=="смыслы" \
               else "Какие конкретные шаги ты видишь здесь?"
    if phase=="plan":
        return "Какой маленький шаг ты готова запланировать на сегодня?" if style["plan"]=="план" \
               else "Какой лёгкий эксперимент попробуешь сначала?"
    return "Расскажи немного больше?"

def personalized_reply(uid:int, text:str, phase:str)->str:
    pr = q("SELECT ei,sn,tf,jp,mbti_type FROM psycho_profile WHERE user_id=%s",(uid,))
    p = pr[0] if pr else {"ei":0.5,"sn":0.5,"tf":0.5,"jp":0.5}
    st = comms_style(p)
    return f"{reflect_emotion(text)}{open_question(phase, st)}"

# ---------- Иерархия интентов/под-интентов ----------
INTENTS: Dict[str, Dict[str, Any]] = {
    # ... (НЕ СКРАЩАЮ — всё из предыдущей версии) ...
}
# (ВСТАВЛЕН полный блок INTENTS из предыдущего сообщения — он длинный, оставляю без изменений)

# --- для краткости ответа здесь опускаю повтор INTENTS ---
# ПРИ ВСТАВКЕ В ФАЙЛ: оставьте полный блок INTENTS из прошлой версии!

INTENT_THRESHOLD = 0.35

def detect_intent(text:str) -> Tuple[Optional[str], Optional[str], float]:
    tl = text.lower()
    best = (None, None, 0.0)
    for intent_key, spec in INTENTS.items():
        base_match = spec["re"].search(tl)
        base_score = 0.0
        if base_match:
            base_score = 0.4 + (0.1 if base_match.start() < 10 else 0.0)
        child_best = (None, 0.0)
        for sub_key, sub in spec.get("children", {}).items():
            m = sub["re"].search(tl)
            if not m: continue
            score = 0.55 + (0.1 if m.start() < 10 else 0.0)
            if score > child_best[1]:
                child_best = (sub_key, min(0.95, score))
        if child_best[1] > 0:
            score = max(base_score, child_best[1])
            if score > best[2]:
                best = (intent_key, child_best[0], score)
        elif base_score > 0 and base_score > best[2]:
            best = (intent_key, None, base_score)
    return best

def topic_question(intent:str, sub:Optional[str], step:int)->str:
    if sub and sub in INTENTS[intent]["children"]:
        prompts = INTENTS[intent]["children"][sub]["prompts"]
    else:
        prompts = [
            "Сформулируй, пожалуйста, один главный вопрос или цель в этой теме.",
            "Что в твоей зоне контроля прямо сейчас?",
            "Какой маленький шаг сделаешь сегодня?"
        ]
    return prompts[min(step, len(prompts)-1)]

def normalize_command(text:str)->Optional[Dict[str,str]]:
    t = text.strip().lower()
    m = re.search(r"(сменим (под-)?тему на|давай про|поговорим про|хочу про)\s+([а-яa-zё\s\-]+)", t)
    if m:
        return {"cmd":"switch","to": m.group(3).strip()}
    if re.search(r"вернемся к|вернёмся к", t):
        return {"cmd":"back"}
    if re.search(r"сброс темы|отмени тему|снять тему", t):
        return {"cmd":"clear"}
    if re.search(r"моя домашка|мои задачи|план", t):
        return {"cmd":"list_tasks"}
    if re.search(r"напомни", t):
        return {"cmd":"remind_now"}
    m2 = re.search(r"(сделано|закрыть)\s+(\d+)", t)
    if m2:
        return {"cmd":"done","id": int(m2.group(2))}
    m3 = re.search(r"(удалить|отменить)\s+(\d+)", t)
    if m3:
        return {"cmd":"delete","id": int(m3.group(2))}
    return None

def resolve_to_intent(label:str)->Tuple[Optional[str], Optional[str]]:
    lab = label.strip().lower()
    for ik, spec in INTENTS.items():
        for sk, ch in spec.get("children", {}).items():
            title = ch["title"].lower()
            if lab in title or any(w and w in title for w in lab.split()):
                return ik, sk
    for ik, spec in INTENTS.items():
        title = spec["title"].lower()
        if lab in title or any(w and w in title for w in lab.split()):
            return ik, None
    return None, None

# ---------- SMART-домашка ----------
DATE_RE = re.compile(r"(\b(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?\b)")
def parse_due_date(t: str) -> date:
    tl = t.lower()
    today = date.today()
    if "сегодня" in tl: return today
    if "завтра" in tl: return today + timedelta(days=1)
    m = DATE_RE.search(tl)
    if m:
        d = int(m.group(2)); mth = int(m.group(3)); y = m.group(4)
        year = today.year if not y else (2000+int(y) if len(y)==2 else int(y))
        try:
            return date(year, mth, d)
        except ValueError:
            return today + timedelta(days=1)
    # по умолчанию — завтра
    return today + timedelta(days=1)

ACTION_RE = re.compile(r"(сдела(ю|ть)|написа(ть|ю)|позвон(ю|ить)|подготов(лю|ить)|отправ(лю|ить)|прочита(ю|ть)|встрет(юсь|иться)|созвон|соберу|разбер(у|ать)|сформулиру(ю|ть)|провед(у|ти))", re.I)

def smartify(raw: str) -> str:
    """Очень мягкая нормализация формулировки шага."""
    txt = raw.strip()
    txt = re.sub(r"\s+", " ", txt)
    # мини-критерии готовности
    if not re.search(r"\b(\d+ ?(мин|час)|черновик|1-2|3|план)\b", txt, re.I):
        txt += " (на 10–20 минут, как черновик)"
    return txt

def create_task(uid:int, text:str, due:date) -> int:
    r = q("INSERT INTO homework_tasks(user_id,text,due_date) VALUES(%s,%s,%s) RETURNING id",
          (uid, text, due))
    return r[0]["id"]

def list_open_tasks(uid:int)->List[Dict[str,Any]]:
    return q("""SELECT id, text, due_date, status
                FROM homework_tasks
                WHERE user_id=%s AND status='open'
                ORDER BY due_date, id""",(uid,)) or []

def mark_task(uid:int, task_id:int, status:str)->bool:
    r = q("UPDATE homework_tasks SET status=%s WHERE user_id=%s AND id=%s RETURNING id",
          (status, uid, task_id))
    return bool(r)

def remindable_tasks() -> List[Dict[str,Any]]:
    return q("""
      SELECT id, user_id, text, due_date, last_reminded_at
      FROM homework_tasks
      WHERE status='open' AND due_date <= CURRENT_DATE
        AND (last_reminded_at IS NULL OR last_reminded_at::date < CURRENT_DATE)
    """) or []

def set_reminded(task_id:int):
    q("UPDATE homework_tasks SET last_reminded_at=NOW() WHERE id=%s",(task_id,))

# ---------- Quality Gate ----------
def quality_ok(s:str)->bool:
    if STOP.search(s): return False
    L = len(s)
    if L < 90 or L > 500: return False
    if "?" not in s: return False
    if not re.search(r"(слышу|вижу|понимаю|рядом|важно|давай|готова|предлагаю)", s.lower()):
        return False
    return True

# ---------- API ----------
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

    # Safety
    if crisis_detect(text):
        reply = ("Я рядом и слышу твою боль. Если нужна срочная поддержка — напиши близким "
                 "или обратись в горячую линию. Что сейчас было бы самым поддерживающим?")
        await tg_send(chat_id, reply)
        q("INSERT INTO dialog_events(user_id,role,text,mi_phase,emotion,relevance,topic,axes) VALUES(%s,'assistant',%s,'support','tense',false,%s,%s)",(uid,reply,"mood",json.dumps({"subtopic":"anxiety"})))
        return {"ok":True}
    if STOP.search(text):
        reply = "Давай оставим чувствительные темы за рамками. О чём тебе важнее поговорить сейчас?"
        await tg_send(chat_id, reply)
        q("INSERT INTO dialog_events(user_id,role,text,mi_phase,emotion,relevance) VALUES(%s,'assistant',%s,'engage','neutral',false)",(uid,reply))
        return {"ok":True}

    # Команды (включая домашку)
    cmd = normalize_command(text)
    if cmd:
        if cmd["cmd"]=="list_tasks":
            tasks = list_open_tasks(uid)
            if not tasks:
                await tg_send(chat_id, "Открытых задач пока нет. Можем сформулировать маленький шаг — просто напиши его.")
            else:
                lines = [f"• #{t['id']} — {t['text']} (до {t['due_date']:%d.%m})" for t in tasks]
                await tg_send(chat_id, "Твой план:\n" + "\n".join(lines) + "\n\nЧтобы закрыть: «сделано ID». Чтобы удалить: «удалить ID».")
            return {"ok":True}
        if cmd["cmd"]=="remind_now":
            tasks = list_open_tasks(uid)
            if not tasks:
                await tg_send(chat_id, "Пока нечего напоминать — открытых задач нет.")
            else:
                soon = [t for t in tasks if t["due_date"] <= date.today()+timedelta(days=1)]
                if not soon:
                    await tg_send(chat_id, "Ближайших задач на сегодня/завтра нет. Но ты можешь добавить новую — просто опиши шаг.")
                else:
                    lines = [f"• #{t['id']} — {t['text']} (до {t['due_date']:%d.%m})" for t in soon]
                    await tg_send(chat_id, "Ближайшее:\n" + "\n".join(lines))
            return {"ok":True}
        if cmd["cmd"]=="done":
            ok = mark_task(uid, cmd["id"], "done")
            await tg_send(chat_id, "Супер! Закрыла задачу." if ok else "Не нашла такую задачу.")
            return {"ok":True}
        if cmd["cmd"]=="delete":
            ok = mark_task(uid, cmd["id"], "deleted")
            await tg_send(chat_id, "Удалено." if ok else "Не нашла такую задачу.")
            return {"ok":True}
        # переключение темы — обрабатывается ниже вместе с прочими командами
        if cmd["cmd"] in {"switch","back","clear"}:
            pass

    # Онбординг/КНО
    st = app_state_get(uid)
    if text.lower() in ("/start","старт","начать") or not st.get("kno_done"):
        if st.get("kno_idx") is None and not st.get("kno_done"):
            kno_start(uid)
            intro = ("Привет 🌿 Я Анима — твой личный психологический ассистент. Помогаю навести ясность, "
                     "снизить стресс и наметить шаги вперёд. Разговоры конфиденциальны, никакого спама — только поддержка 💛\n\n"
                     "Поехали? Отвечай цифрой 1 или 2, можно своими словами 🙂")
            await tg_send(chat_id, intro + "\n\n" + KNO[0][1])
            q("INSERT INTO dialog_events(user_id,role,text,mi_phase) VALUES(%s,'assistant',%s,'engage')",(uid,intro))
            return {"ok": True}

        nxt = kno_step(uid, text)
        if nxt is None:
            prof = q("SELECT ei,sn,tf,jp,confidence,mbti_type FROM psycho_profile WHERE user_id=%s",(uid,))[0]
            conf = int((prof["confidence"] or 0)*100)
            mbti = prof.get("mbti_type") or "черновой профиль уточнится"
            reply = (f"Спасибо, я лучше понимаю, как с тобой говорить 💛\n"
                     f"Пока это черновой профиль: {mbti}. Точность будет расти по ходу диалога (≈{conf}%).\n\n"
                     "Расскажи коротко — с чем хочешь сегодня поработать или о чём поговорить?")
            await tg_send(chat_id, reply)
            q("INSERT INTO dialog_events(user_id,role,text,mi_phase) VALUES(%s,'assistant',%s,'engage')",(uid,reply))
            app_state_set(uid, {"topic": None, "subtopic": None, "topic_step":0, "topic_locked":False})
            return {"ok": True}
        else:
            await tg_send(chat_id, nxt + "\n\nОтветь 1 или 2, можно словами.")
            q("INSERT INTO dialog_events(user_id,role,text,mi_phase) VALUES(%s,'assistant',%s,'engage')",(uid,nxt))
            return {"ok": True}

    # Профиль по реплике
    emo = detect_emotion(text)
    rel, axes, anchors = classify_relevance(text)
    if rel: update_profile(uid, axes, anchors)

    # Интент/под-интент
    st = app_state_get(uid)
    current_topic = st.get("topic")
    current_sub   = st.get("subtopic")
    topic_step    = int(st.get("topic_step", 0))
    topic_locked  = bool(st.get("topic_locked", False))

    # явные команды переключения тем
    if cmd and cmd.get("cmd") in {"switch","back","clear"}:
        prev_topic, prev_sub = st.get("topic"), st.get("subtopic")
        if cmd["cmd"]=="switch":
            intent, sub = resolve_to_intent(cmd["to"])
            if not intent and not sub:
                await tg_send(chat_id, "Уточни: работа, отношения, здоровье, настроение, финансы или учёба/продуктивность (можно с под-темой).")
                return {"ok":True}
            app_state_set(uid, {"topic":intent, "subtopic":sub, "topic_step":0, "topic_locked":True, "prev_topic":prev_topic, "prev_subtopic":prev_sub})
            title = INTENTS[intent]["children"][sub]["title"] if sub else INTENTS[intent]["title"]
            reply = f"Окей, переключаюсь на «{title}». {topic_question(intent, sub, 0)}"
            await tg_send(chat_id, reply)
            q("INSERT INTO dialog_events(user_id,role,text,mi_phase,topic,axes) VALUES(%s,'assistant',%s,'focus',%s,%s)",(uid,reply,intent,json.dumps({"subtopic":sub})))
            return {"ok":True}
        if cmd["cmd"]=="back":
            bt, bs = st.get("prev_topic"), st.get("prev_subtopic")
            if bt:
                app_state_set(uid, {"topic":bt, "subtopic":bs, "topic_step":0, "topic_locked":True, "prev_topic":None, "prev_subtopic":None})
                title = INTENTS[bt]["children"][bs]["title"] if bs else INTENTS[bt]["title"]
                reply = f"Вернёмся к «{title}». {topic_question(bt, bs, 0)}"
                await tg_send(chat_id, reply)
                q("INSERT INTO dialog_events(user_id,role,text,mi_phase,topic,axes) VALUES(%s,'assistant',%s,'focus',%s,%s)",(uid,reply,bt,json.dumps({"subtopic":bs})))
                return {"ok":True}
            await tg_send(chat_id, "Пока не к чему возвращаться — тема не менялась. О чём продолжим?")
            return {"ok":True}
        if cmd["cmd"]=="clear":
            app_state_set(uid, {"topic_locked":False})
            await tg_send(chat_id, "Сняла фиксацию темы. Выбирай новую — «давай про …».")
            return {"ok":True}

    intent, sub, score = detect_intent(text)

    if not topic_locked and intent and score >= INTENT_THRESHOLD:
        current_topic, current_sub = intent, sub
        topic_step = 0
        app_state_set(uid, {"topic": current_topic, "subtopic": current_sub, "topic_step": topic_step})

    last = q("SELECT mi_phase, topic, axes FROM dialog_events WHERE user_id=%s ORDER BY id DESC LIMIT 1",(uid,))
    last_phase = last[0]["mi_phase"] if last else "engage"

    going_off = False
    if current_topic and intent and intent != current_topic: going_off = True
    if current_sub and sub and sub != current_sub: going_off = True

    # План-режим: если пользователь формулирует действие — сохраняем «домашку»
    if choose_phase(last_phase, emo, text) == "plan" and ACTION_RE.search(text):
        due = parse_due_date(text)
        step_text = smartify(text)
        task_id = create_task(uid, step_text, due)
        reply = (f"Записала: #{task_id} — {step_text}\nСрок: {due:%d.%m}. "
                 f"Напомню в день дедлайна. Можешь написать «моя домашка», «сделано {task_id}» или «удалить {task_id}».")
        await tg_send(chat_id, reply)
        q("INSERT INTO dialog_events(user_id,role,text,mi_phase) VALUES(%s,'assistant',%s,'plan')",(uid,reply))
        return {"ok":True}

    # Есть зафиксированная тема — ведём по ней
    if current_topic:
        reminded = st.get("topic_reminded", False)
        if going_off and not reminded:
            app_state_set(uid, {"topic_reminded": True})
            title = INTENTS[current_topic]["children"][current_sub]["title"] if current_sub else INTENTS[current_topic]["title"]
            reply = (f"Слышу тебя 💛 Кажется, мы чуть ушли в сторону. "
                     f"Давай сначала завершим разговор о «{title}». Если захочешь сменить под-тему — скажи «сменим под-тему на …».")
            await tg_send(chat_id, reply)
            q("INSERT INTO dialog_events(user_id,role,text,mi_phase,topic,axes) VALUES(%s,'assistant',%s,'focus',%s,%s)",(uid,reply,current_topic,json.dumps({"subtopic":current_sub})))
            return {"ok":True}
        else:
            app_state_set(uid, {"topic_reminded": False})

        phase = choose_phase(last_phase, emo, text)
        title = INTENTS[current_topic]["children"][current_sub]["title"] if current_sub else INTENTS[current_topic]["title"]
        lead  = topic_question(current_topic, current_sub, topic_step)
        draft = f"{reflect_emotion(text)}Продолжим «{title}». {lead}"
        if phase=="plan":
            draft += "\n\nЕсли сформулируешь маленький шаг и срок (сегодня/завтра/дата), я запишу и напомню."
        if not quality_ok(draft):
            draft = f"Продолжим «{title}». {lead}"
        await tg_send(chat_id, draft)
        q("""INSERT INTO dialog_events(user_id,role,text,mi_phase,emotion,relevance,axes,topic)
             VALUES(%s,'user',%s,%s,%s,%s,%s,%s)""",
          (uid, text, phase, emo, rel, json.dumps({**(axes if rel else {}), "subtopic":current_sub}), current_topic))
        q("""INSERT INTO dialog_events(user_id,role,text,mi_phase,emotion,relevance,topic,axes)
             VALUES(%s,'assistant',%s,%s,%s,%s,%s,%s)""",
          (uid, draft, phase, emo, rel, current_topic, json.dumps({"subtopic":current_sub})))
        app_state_set(uid, {"topic_step": topic_step + 1})
        return {"ok":True}

    # Темы нет — предложим догадку
    phase = choose_phase(last_phase, emo, text)
    draft = personalized_reply(uid, text, phase)
    if intent and score >= INTENT_THRESHOLD:
        title = INTENTS[intent]["children"][sub]["title"] if sub else INTENTS[intent]["title"]
        draft = (f"{reflect_emotion(text)}Похоже, речь про «{title}». "
                 f"Начнём с простого: {topic_question(intent, sub, 0)} Если это не то — скажи «сменим под-тему на …».")
        if phase=="plan":
            draft += "\n\nОпиши маленький шаг и срок (сегодня/завтра/дата) — запишу и напомню."
        app_state_set(uid, {"topic": intent, "subtopic": sub, "topic_step": 1})
    if not quality_ok(draft):
        draft = "Слышу тебя. Что здесь для тебя главное?"
    await tg_send(chat_id, draft)
    q("""INSERT INTO dialog_events(user_id,role,text,mi_phase,emotion,relevance,axes)
         VALUES(%s,'user',%s,%s,%s,%s,%s)""",
      (uid, text, phase, emo, rel, json.dumps(axes if rel else {})))
    q("""INSERT INTO dialog_events(user_id,role,text,mi_phase,emotion,relevance,axes)
         VALUES(%s,'assistant',%s,%s,%s,%s,%s)""",
      (uid, draft, phase, emo, rel, json.dumps({"suggested_intent":intent,"subtopic":sub,"score":score})))
    return {"ok":True}

# ---------- Daily topics ----------
@app.post("/jobs/daily-topics/run-for/{uid}")
async def daily_topics_for(uid: int, payload: Dict[str, Any] = None):
    p = q("SELECT ei,sn,tf,jp FROM psycho_profile WHERE user_id=%s",(uid,))
    p = p[0] if p else None

    topics: List[Dict[str,str]] = []
    if p and p["jp"] >= 0.5:
        topics.append({"title":"Один маленький шаг на сегодня", "why":"тебе помогает план и порядок"})
    else:
        topics.append({"title":"Лёгкий эксперимент на сегодня", "why":"тебе помогает гибкость и проба"})
    if p and p["sn"] >= 0.5:
        topics.append({"title":"Какие конкретные шаги приблизят цель", "why":"конкретика снижает напряжение"})
    else:
        topics.append({"title":"Какой смысл ты видишь сейчас", "why":"смысл даёт энергию двигаться"})
    topics.append({"title":"Что помогает тебе восстанавливаться", "why":"поддержка ресурса важна ежедневно"})

    q("""INSERT INTO daily_topics(user_id, topics)
         VALUES(%s,%s)
         ON CONFLICT (user_id) DO UPDATE SET topics=EXCLUDED.topics""", (uid, json.dumps(topics)))
    return {"user_id": uid, "topics": topics}

# ---------- Reminders / Digest ----------
@app.post("/jobs/reminders/run")
async def jobs_reminders():
    tasks = remindable_tasks()
    for t in tasks:
        days_over = (date.today() - t["due_date"]).days
        if days_over > 0:
            msg = f"Напоминание: #{t['id']} — {t['text']} (срок был {t['due_date']:%d.%m}). Если сделал(а) — «сделано {t['id']}»."
        else:
            msg = f"Сегодня дедлайн: #{t['id']} — {t['text']}. Когда планируешь выполнить? После — «сделано {t['id']}»."
        await tg_send(t["user_id"], msg)
        set_reminded(t["id"])
    return {"sent": len(tasks)}

@app.post("/jobs/daily-digest/run")
async def jobs_daily_digest():
    # простая рассылка: тем, у кого есть открытые задачи
    users = q("SELECT DISTINCT user_id FROM homework_tasks WHERE status='open'")
    cnt = 0
    for u in users or []:
        uid = u["user_id"]
        tasks = list_open_tasks(uid)
        today_tasks = [t for t in tasks if t["due_date"] == date.today()]
        if not today_tasks: continue
        lines = [f"• #{t['id']} — {t['text']} (до {t['due_date']:%d.%m})" for t in today_tasks]
        msg = "Доброе утро 🌞 Вот что запланировано на сегодня:\n" + "\n".join(lines) + "\n\nЯ рядом. После выполнения — «сделано ID»."
        await tg_send(uid, msg)
        cnt += 1
    return {"digests_sent": cnt}

# ---------- Reports ----------
def auth_reports(x_token: str) -> bool:
    return (not REPORTS_TOKEN) or (x_token == REPORTS_TOKEN)

@app.get("/reports/summary")
async def reports_summary(x_token: str = Header(default="")):
    if not auth_reports(x_token): return {"error":"unauthorized"}

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

@app.get("/reports/user/{uid}")
async def reports_user(uid: int, x_token: str = Header(default="")):
    if not auth_reports(x_token): return {"error":"unauthorized"}
    prof = q("SELECT * FROM psycho_profile WHERE user_id=%s",(uid,))
    last_events = q("""
      SELECT role, text, emotion, mi_phase, relevance, topic, axes, created_at
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
    return {"profile": prof[0] if prof else {}, "last_events": last_events or [], "quality_14d": quality or []}

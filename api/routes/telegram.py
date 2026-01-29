from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from api.services.telegram import tg_send
from api.services.dialogue import (
    STOP,
    app_state,
    build_reply,
    compose_menu,
    crisis_detect,
    detect_emotion,
    ensure_user,
    get_profile_style,
    idempotency_guard,
    kno_next,
    kno_register,
    kno_start,
    log_event,
    not_duplicate,
    quality_score,
    set_state,
)

logger = logging.getLogger("anima")

router = APIRouter()

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")


class TelegramUpdate(BaseModel):
    update_id: Optional[int] = None
    message: Optional[Dict[str, Any]] = None


@router.post("/webhook/telegram")
async def webhook(update: TelegramUpdate, request: Request) -> Dict[str, Any]:
    if WEBHOOK_SECRET:
        got = request.headers.get("X-Webhook-Secret", "")
        if got != WEBHOOK_SECRET:
            logger.warning("Webhook forbidden: bad secret. ip=%s", request.client.host if request.client else None)
            raise HTTPException(status_code=401, detail="Unauthorized")
    else:
        logger.warning("WEBHOOK_SECRET is not set. Webhook endpoint is not protected.")

    try:
        ok = await idempotency_guard(update.update_id)
        if not ok:
            return {"ok": True}
    except Exception:
        logger.exception("Idempotency check failed (update_id=%s)", update.update_id)
        raise HTTPException(status_code=503, detail="DB unavailable")

    if not update.message:
        return {"ok": True}

    msg = update.message
    chat_id = int(msg["chat"]["id"])
    uid = chat_id
    text = (msg.get("text") or "").strip()

    u = msg.get("from", {}) or {}
    try:
        await ensure_user(uid, u.get("username"), u.get("first_name"), u.get("last_name"))
    except Exception:
        logger.exception("ensure_user failed (uid=%s)", uid)

    logger.info("telegram_update chat_id=%s text_len=%s", chat_id, len(text))

    # toggles
    if text.lower().startswith("/humor"):
        on = any(w in text.lower() for w in ["on", "вкл", "да", "true"])
        st = await app_state(uid)
        st["humor_on"] = on
        await set_state(uid, st)
        await tg_send(chat_id, "Юмор включён 😊" if on else "Юмор выключен 👍")
        return {"ok": True}

    st = await app_state(uid)
    if re.search(r"\bпошути\b|немного юмора|чуть иронии", text.lower()):
        st["humor_on"] = True
        await set_state(uid, st)

    # Safety
    if crisis_detect(text):
        reply = (
            "Я рядом и слышу твою боль. Если нужна поддержка прямо сейчас — "
            "обратись к близким или в службу помощи. "
            "Что сейчас было бы самым бережным для тебя?"
        )
        await tg_send(chat_id, reply)
        await log_event(uid, "assistant", reply, "support", "tense", False)
        return {"ok": True}

    if STOP.search(text):
        reply = "Давай оставим чувствительные темы за рамками. О чём тебе важнее поговорить сейчас?"
        await tg_send(chat_id, reply)
        await log_event(uid, "assistant", reply, "engage", "neutral", False)
        return {"ok": True}

    # Greeting & name
    name = st.get("name")
    intro_done = bool(st.get("intro_done", False))

    if text.lower() in ("/start", "start"):
        await set_state(uid, {"intro_done": False, "name": None, "kno_idx": None, "kno_done": False, "menu_map": {}})
        greet = (
            "Привет 🌿 Я Анима — твой личный психологический ассистент. "
            "Я помогаю навести ясность, снизить стресс и наметить шаги вперёд. "
            "Наши разговоры конфиденциальны, никакого спама — только поддержка 💛\n\n"
            "Как мне к тебе обращаться?"
        )
        await tg_send(chat_id, greet)
        await log_event(uid, "assistant", greet, "engage")
        return {"ok": True}

    if not intro_done:
        if not name:
            if len(text) <= 40 and not re.search(r"\d", text):
                await set_state(uid, {"name": text})
                prompt = "Как ты сейчас? Выбери слово: спокойно, напряжённо, растерянно — или опиши по-своему."
                await tg_send(chat_id, f"Рада знакомству, {text}! ✨")
                await tg_send(chat_id, prompt)
                return {"ok": True}
            await tg_send(chat_id, "Как мне к тебе обращаться? Коротко — одним словом 🙂")
            return {"ok": True}

        await set_state(uid, {"intro_done": True})
        await tg_send(chat_id, "Спасибо! Начнём с короткой анкеты (6 вопросов). Отвечай 1 или 2, можно словами.")
        await kno_start(uid)
        nxt = await kno_next(uid)
        if nxt:
            await tg_send(chat_id, nxt)
        return {"ok": True}

    # KNO flow
    st = await app_state(uid)
    if not st.get("kno_done"):
        nxt = await kno_register(uid, text)
        if nxt is None:
            summary = (
                "Спасибо, я лучше понимаю, как с тобой говорить 💛\n"
                "Уверенность 40%\n"
                "Пока это черновой профиль. Он будет уточняться по ходу диалога.\n\n"
                "Расскажи коротко — с чем хочешь сегодня поработать или о чём поговорить?\n\n"
                + (await compose_menu(uid))
            )
            await tg_send(chat_id, summary)
            await log_event(uid, "assistant", summary, "engage")
            return {"ok": True}

        await tg_send(chat_id, nxt)
        await log_event(uid, "assistant", nxt, "engage")
        return {"ok": True}

    # Free dialogue
    emo = detect_emotion(text)
    humor_on = bool(st.get("humor_on"))
    style = await get_profile_style(uid)

    menu_choice = None
    mm = (await app_state(uid)).get("menu_map") or {}
    if (text or "").strip() in mm:
        from api.services.dialogue import try_menu_choice  # local to keep exports minimal

        menu_choice = await try_menu_choice(uid, text, style, humor_on)

    if menu_choice:
        draft = menu_choice
    else:
        draft = await build_reply(uid, text, humor_on)

    if quality_score(text, draft) < 0.55:
        draft = await compose_menu(uid)

    draft = await not_duplicate(uid, draft)
    await tg_send(chat_id, draft)

    await log_event(uid, "user", text, "engage", emo, True)
    await log_event(uid, "assistant", draft, "engage", emo, True)

    return {"ok": True}

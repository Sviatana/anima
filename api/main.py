import os
import re
from typing import Any, Dict, Optional
from fastapi import FastAPI, Request
from pydantic import BaseModel
from dotenv import load_dotenv
import httpx

load_dotenv()
app = FastAPI(title="ANIMA Minimal API")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

STOP_TOPICS = re.compile(r"(политик|религ|насили|медицинск|суицид)", re.IGNORECASE)

class TelegramUpdate(BaseModel):
    update_id: Optional[int] = None
    message: Optional[Dict[str, Any]] = None

def crisis_detect(text: str) -> bool:
    return bool(re.search(r"(не хочу жить|самоповрежд|отчаяни|суицид)", text, re.IGNORECASE))

async def tg_send(chat_id: int, text: str):
    if not TELEGRAM_TOKEN:
        print(f"[DRY RUN] -> {chat_id}: {text}")
        return
    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text},
        )

@app.get("/")
async def root():
    return {"ok": True, "service": "anima-min"}

@app.post("/webhook/telegram")
async def webhook(update: TelegramUpdate, request: Request):
    if not update.message:
        return {"ok": True}

    msg = update.message
    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()

    if crisis_detect(text):
        reply = "Я рядом и слышу тебя. Если нужна срочная поддержка обратись в службу помощи или к близким. Что сейчас было бы для тебя самым поддерживающим"
        await tg_send(chat_id, reply)
        return {"ok": True}

    if STOP_TOPICS.search(text):
        reply = "Давай оставим чувствительные темы за рамками. О чем тебе сейчас важнее поговорить"
        await tg_send(chat_id, reply)
        return {"ok": True}

    reply = "Я с тобой и слышу твои чувства. Что сейчас для тебя самое важное"
    await tg_send(chat_id, reply)
    return {"ok": True}
    

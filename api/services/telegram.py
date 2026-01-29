from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger("anima")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")


async def tg_send(chat_id: int, text: str) -> None:
    if not TELEGRAM_TOKEN:
        logger.info("[DRY RUN] -> %s: %s", chat_id, (text or "")[:300])
        return

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": text},
            )
            r.raise_for_status()
    except Exception:
        logger.exception("Telegram send failed (chat_id=%s)", chat_id)

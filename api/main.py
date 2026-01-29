from __future__ import annotations

import logging
import os
from typing import Any, Dict

from dotenv import load_dotenv
from fastapi import FastAPI

from api.db import create_pool
from api.routes.telegram import router as telegram_router

load_dotenv()

APP_TITLE = os.getenv("APP_TITLE", "ANIMA 2.0")
DB_URL = os.getenv("DATABASE_URL", "")

logger = logging.getLogger("anima")
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

app = FastAPI(title=APP_TITLE)
app.include_router(telegram_router)


@app.get("/")
async def root() -> Dict[str, Any]:
    return {"ok": True, "service": "anima"}


@app.on_event("startup")
async def startup() -> None:
    if not DB_URL:
        logger.warning("DATABASE_URL is not set. DB features will fail.")
        return
    try:
        app.state.db_pool = await create_pool(DB_URL)
        logger.info("DB pool created.")
    except Exception:
        logger.exception("Failed to create DB pool.")
        raise


@app.on_event("shutdown")
async def shutdown() -> None:
    pool = getattr(app.state, "db_pool", None)
    if pool:
        await pool.close()
        logger.info("DB pool closed.")

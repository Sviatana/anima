from __future__ import annotations

import os
import logging
from typing import Any, Dict, List, Optional

import asyncpg

logger = logging.getLogger("anima")


async def create_pool(dsn: str) -> asyncpg.Pool:
    return await asyncpg.create_pool(
        dsn=dsn,
        min_size=int(os.getenv("DB_POOL_MIN", "1")),
        max_size=int(os.getenv("DB_POOL_MAX", "5")),
        command_timeout=float(os.getenv("DB_COMMAND_TIMEOUT", "15")),
    )


def _pool() -> asyncpg.Pool:
    # set in api/main.py on startup
    from api.main import app  # local import to avoid circular at import time

    pool = getattr(app.state, "db_pool", None)
    if pool is None:
        raise RuntimeError("DB pool is not initialized")
    return pool


async def fetch(sql: str, *params: Any) -> List[Dict[str, Any]]:
    pool = _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
        return [dict(r) for r in rows]


async def fetchval(sql: str, *params: Any) -> Any:
    pool = _pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(sql, *params)


async def execute(sql: str, *params: Any) -> str:
    pool = _pool()
    async with pool.acquire() as conn:
        return await conn.execute(sql, *params)


async def mark_update_processed(update_id: int) -> bool:
    status = await execute(
        "INSERT INTO processed_updates(update_id) VALUES($1) ON CONFLICT DO NOTHING",
        update_id,
    )
    return status.endswith(" 1")

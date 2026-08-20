"""raw 表：从内存绕一圈数据库，插入成功后由网关广播。

满 100 行直接清除表格。不记状态，不是保序也不是防丢。
运行路径不读这张表。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from qqbot.core.ids import new_event_id
from qqbot.core.logging import get_logger
from qqbot.core.time import china_now
from qqbot.models.raw_event import RawEvent

logger = get_logger(__name__)

SessionFactory = Callable[[], AsyncSession]

_RAW_CLEAR_AFTER = 100


async def insert_raw_event(
    session_factory: SessionFactory,
    *,
    channel: str,
    payload: Any,
    received_at: datetime | None = None,
) -> bool:
    """INSERT one raw dump. Return True only when the row committed.

    Broadcast happens at the caller after this returns True.
    """
    if not isinstance(payload, dict):
        logger.warning("[raw_event_log] skip non-dict payload channel={}", channel)
        return False
    label = str(channel or "").strip()
    if not label:
        logger.warning("[raw_event_log] skip empty channel")
        return False
    try:
        stmt = pg_insert(RawEvent).values(
            raw_id=new_event_id(),
            channel=label,
            received_at=received_at if received_at is not None else china_now(),
            raw_payload=payload,
        )
        async with session_factory() as session:
            await session.execute(stmt)
            await session.commit()
    except Exception as exc:
        logger.warning("[raw_event_log] insert failed channel={} err={}", label, exc)
        return False
    await _clear_if_full(session_factory)
    return True


async def copy_raw_event(
    session_factory: SessionFactory,
    *,
    channel: str,
    payload: Any,
    received_at: datetime | None = None,
) -> None:
    """Compat wrapper. New path uses insert_raw_event and checks the bool."""
    await insert_raw_event(
        session_factory,
        channel=channel,
        payload=payload,
        received_at=received_at,
    )


async def _clear_if_full(session_factory: SessionFactory) -> None:
    try:
        async with session_factory() as session:
            result = await session.execute(
                select(func.count()).select_from(RawEvent)
            )
            count_fn = getattr(result, "scalar_one", None)
            if not callable(count_fn):
                return
            n = count_fn()
            if asyncio_isawaitable(n):
                n = await n  # type: ignore[misc]
            if not isinstance(n, int) or n < _RAW_CLEAR_AFTER:
                return
            await session.execute(delete(RawEvent))
            await session.commit()
    except Exception as exc:
        logger.warning("[raw_event_log] clear failed err={}", exc)


def asyncio_isawaitable(value: Any) -> bool:
    from inspect import isawaitable

    return isawaitable(value)

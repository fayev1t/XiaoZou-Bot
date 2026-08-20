"""群记忆表：一个群一行，UPDATE 覆盖。"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from qqbot.core.logging import get_logger
from qqbot.core.time import china_now
from qqbot.models.group_memory import GroupMemory

logger = get_logger(__name__)

SessionFactory = Callable[[], AsyncSession]


async def upsert_group_memory(
    session_factory: SessionFactory,
    *,
    group_id: int,
    content: str,
) -> None:
    now = china_now()
    stmt = (
        pg_insert(GroupMemory)
        .values(group_id=group_id, content=content, updated_at=now)
        .on_conflict_do_update(
            index_elements=["group_id"],
            set_={"content": content, "updated_at": now},
        )
    )
    try:
        async with session_factory() as session:
            await session.execute(stmt)
            await session.commit()
    except Exception as exc:
        logger.warning(
            "[group_memory] upsert failed group_id={} err={}", group_id, exc
        )


async def load_group_memory(
    session_factory: SessionFactory, group_id: int
) -> str | None:
    stmt = select(GroupMemory.content).where(GroupMemory.group_id == group_id)
    try:
        async with session_factory() as session:
            result = await session.execute(stmt)
            return result.scalars().first()
    except Exception as exc:
        logger.warning(
            "[group_memory] load failed group_id={} err={}", group_id, exc
        )
        return None

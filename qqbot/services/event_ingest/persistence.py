"""Append a SystemEvent into agent_events using ON CONFLICT DO NOTHING.

Contract: 开发文档/v2.0/EventIngest契约.md §4.2
"""

from __future__ import annotations

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from qqbot.models.agent_event import AgentEvent
from qqbot.services.event_ingest.system_event import SystemEvent


async def persist_event(session: AsyncSession, event: SystemEvent) -> bool:
    """Insert an event; return True on insert, False on idempotency-key conflict.

    Single-event transaction. See EventIngest契约.md §4.2.
    """
    stmt = (
        pg_insert(AgentEvent)
        .values(
            event_id=event.event_id,
            occurred_at=event.occurred_at,
            origin=event.origin,
            type=event.type,
            scope=event.scope,
            group_id=event.group_id,
            user_id=event.user_id,
            visibility=event.visibility,
            correlation_id=event.correlation_id,
            causation_id=event.causation_id,
            idempotency_key=event.idempotency_key,
            payload=event.payload,
            raw=event.raw,
        )
        .on_conflict_do_nothing(index_elements=["idempotency_key"])
    )

    result = await session.execute(stmt)
    await session.commit()
    return (result.rowcount or 0) > 0

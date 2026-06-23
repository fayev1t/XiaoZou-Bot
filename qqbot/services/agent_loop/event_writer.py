"""Internal event writers for agent.* and runtime.* events.

External events go through EventIngest (with idempotency_key + ON CONFLICT).
Internal events (agent.* / runtime.*) come from the loop itself; no external
dedup is needed because they have unique event_ids generated locally.

Contract: 开发文档/v2.0/事件系统设计.md §2, §4.2-4.3
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from sqlalchemy.ext.asyncio import AsyncSession

from qqbot.core.ids import new_event_id
from qqbot.core.time import china_now
from qqbot.services.event_ingest.persistence import persist_event
from qqbot.services.event_ingest.system_event import SystemEvent

SessionFactory = Callable[[], AsyncSession]


def parse_scope_key(scope_key: str) -> tuple[str, int | None, int | None]:
    """scope_key → (scope, group_id, user_id).

    Accepts:
    - "system"            → ("system", None, None)
    - "group:<int>"       → ("group", group_id, None)
    - "private:<int>"     → ("private", None, user_id)
    """
    if scope_key == "system":
        return "system", None, None
    if scope_key.startswith("group:"):
        return "group", int(scope_key.split(":", 1)[1]), None
    if scope_key.startswith("private:"):
        return "private", None, int(scope_key.split(":", 1)[1])
    raise ValueError(f"invalid scope_key: {scope_key!r}")


async def write_internal_event(
    session_factory: SessionFactory,
    *,
    origin: str,
    event_type: str,
    scope_key: str,
    visibility: str,
    correlation_id: str,
    causation_id: str | None,
    payload: dict,
    occurred_at: datetime | None = None,
) -> str:
    """Append a single internal (agent.* / runtime.*) event.

    Returns the generated event_id. Callers use it for downstream causation
    links (e.g. tool_called.causation_id = decision_emitted.event_id).
    """
    scope, group_id, user_id = parse_scope_key(scope_key)
    event_id = new_event_id()
    sys_event = SystemEvent(
        event_id=event_id,
        occurred_at=occurred_at or china_now(),
        origin=origin,
        type=event_type,
        scope=scope,
        group_id=group_id,
        user_id=user_id,
        visibility=visibility,
        correlation_id=correlation_id,
        causation_id=causation_id,
        idempotency_key=None,
        payload=payload,
        raw=None,
    )
    async with session_factory() as session:
        await persist_event(session, sys_event)
    # 读模型双写：事件落定**之后**，独立事务 best-effort 投影进 agent_tasks。
    # 刻意不与事件写同事务 —— 派生视图的失败不能拖垮 append-only 事件流的持久性
    # （详见 task_store.apply_task_event_safe）。懒导入避免给非 task 写路径平白
    # 拉进 task_store + 模型。
    if event_type.startswith("agent.task_"):
        from qqbot.services.agent_loop.task_store import apply_task_event_safe

        await apply_task_event_safe(
            session_factory,
            event_type=event_type,
            scope_key=scope_key,
            occurred_at=sys_event.occurred_at,
            payload=payload,
        )
    return event_id


async def write_runtime_event(
    session_factory: SessionFactory,
    *,
    event_type: str,
    scope_key: str,
    visibility: str,
    correlation_id: str,
    causation_id: str | None,
    payload: dict,
) -> str:
    return await write_internal_event(
        session_factory,
        origin="runtime",
        event_type=event_type,
        scope_key=scope_key,
        visibility=visibility,
        correlation_id=correlation_id,
        causation_id=causation_id,
        payload=payload,
    )


async def write_agent_event(
    session_factory: SessionFactory,
    *,
    event_type: str,
    scope_key: str,
    correlation_id: str,
    causation_id: str | None,
    payload: dict,
) -> str:
    # agent.* 事件按契约 §4.2 全部 visibility=agent_visible
    return await write_internal_event(
        session_factory,
        origin="agent",
        event_type=event_type,
        scope_key=scope_key,
        visibility="agent_visible",
        correlation_id=correlation_id,
        causation_id=causation_id,
        payload=payload,
    )

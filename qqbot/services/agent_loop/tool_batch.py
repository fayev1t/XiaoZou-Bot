"""Shared tool-batch completion detection for inline and worker execution."""

from __future__ import annotations

from typing import Any, Callable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from qqbot.core.logging import get_logger
from qqbot.services.agent_loop.event_writer import write_runtime_event

logger = get_logger(__name__)

SessionFactory = Callable[[], AsyncSession]

_BATCH_STATUS_QUERY = text(
    """
    SELECT
        COUNT(*) AS called,
        COUNT(*) FILTER (
            WHERE EXISTS (
                SELECT 1 FROM agent_events d
                WHERE d.causation_id = r.event_id
                  AND d.type IN ('agent.tool_result', 'agent.tool_failed')
            )
        ) AS terminal
    FROM agent_events r
    WHERE r.type = 'agent.tool_called'
      AND r.payload->>'tool_batch_id' = :tool_batch_id
    """
)

_BATCH_COMPLETED_EXISTS_QUERY = text(
    """
    SELECT 1
    FROM agent_events
    WHERE type = 'runtime.tool_batch_completed'
      AND payload->>'tool_batch_id' = :tool_batch_id
    LIMIT 1
    """
)


async def maybe_close_tool_batch(  # noqa: PLR0913
    session_factory: SessionFactory,
    *,
    supervisor: Any | None,
    scope_key: str,
    tool_batch_id: str,
    tool_batch_size: int | None,
    terminal_event_id: str,
    correlation_id: str,
) -> bool:
    """收口已全部 terminal 的工具批次，并在事件落库后通知对应 scope。

    inline 工具由 AgentLoop 调用，worker 工具由 ToolWorker 调用；谁最后补齐
    整批 terminal，谁负责写唯一的 ``runtime.tool_batch_completed``。返回值
    表示调用时批次是否已经满足收口条件。
    """
    async with session_factory() as session:
        result = await session.execute(
            _BATCH_STATUS_QUERY,
            {"tool_batch_id": tool_batch_id},
        )
        row = result.mappings().first()
    called = int(row["called"] or 0) if row else 0
    terminal = int(row["terminal"] or 0) if row else 0
    if called == 0 or terminal < called:
        return False
    if tool_batch_size is not None and called < tool_batch_size:
        # AgentLoop 尚未写完同批后续 tool_called，不能被写间隙误判为收口。
        return False

    async with session_factory() as session:
        result = await session.execute(
            _BATCH_COMPLETED_EXISTS_QUERY,
            {"tool_batch_id": tool_batch_id},
        )
        already_written = result.first() is not None
    if not already_written:
        await write_runtime_event(
            session_factory,
            event_type="runtime.tool_batch_completed",
            scope_key=scope_key,
            visibility="agent_visible",
            correlation_id=correlation_id,
            causation_id=terminal_event_id,
            payload={
                "tool_batch_id": tool_batch_id,
                "tool_count": called,
                "tool_batch_size": tool_batch_size,
            },
        )

    if supervisor is None:
        return True
    notify = getattr(supervisor, "notify_tool_batch_completed", None)
    try:
        if notify is not None:
            await notify(scope_key, tool_batch_id)
        else:
            await supervisor.wake(scope_key)
        logger.info(
            "[tool_batch] completed: scope={} batch={} tools={}",
            scope_key,
            tool_batch_id,
            called,
        )
    except Exception as exc:  # noqa: BLE001 — completion 已落库，通知 best-effort
        logger.warning(
            "[tool_batch] completion notify failed: scope={} batch={}: {}",
            scope_key,
            tool_batch_id,
            exc,
        )
    return True

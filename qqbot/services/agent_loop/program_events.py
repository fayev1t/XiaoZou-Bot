"""Append-only event protocol for program execution and crash recovery."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qqbot.core.ids import new_event_id
from qqbot.models.agent_event import AgentEvent
from qqbot.services.agent_loop.event_writer import (
    AgentEventWrite,
    parse_scope_key,
    write_agent_event,
    write_agent_events,
)
from qqbot.services.agent_loop.tool_registry import ToolOutcome

if TYPE_CHECKING:
    from datetime import datetime

SessionFactory = Callable[[], AsyncSession]

_TOOL_TERMINALS = frozenset({"agent.tool_result", "agent.tool_failed"})
_PROGRAM_TERMINALS = frozenset({"agent.program_completed", "agent.program_failed"})
_RECOVERY_TYPES = tuple(
    {
        "agent.decision_emitted",
        "agent.tool_called",
        *_TOOL_TERMINALS,
        *_PROGRAM_TERMINALS,
    }
)


@dataclass(frozen=True)
class EffectCallHandle:
    tool_call_id: str
    called_event_id: str
    decision_id: str
    tool_name: str
    task_id: str | None
    call_site: str
    occurrence: int


@dataclass(frozen=True)
class RecoveryReport:
    tool_calls_closed: int = 0
    programs_closed: int = 0


async def begin_effect_call(  # noqa: PLR0913
    session_factory: SessionFactory,
    *,
    scope_key: str,
    correlation_id: str,
    decision_id: str,
    tool_name: str,
    arguments: dict,
    task_id: str | None,
    triggered_by_event_id: str | None,
    bot_role: str | None,
    call_site: str,
    occurrence: int,
    occurred_at: datetime | None = None,
) -> EffectCallHandle:
    """Transaction 1: persist intent before any external side effect."""
    tool_call_id = new_event_id()
    called_event_id = new_event_id()
    payload = {
        "tool_call_id": tool_call_id,
        "tool_name": tool_name,
        "arguments": arguments,
        "task_id": task_id,
        "triggered_by_event_id": triggered_by_event_id,
        "bot_role": bot_role,
        "program_call_site": call_site,
        "program_occurrence": occurrence,
    }
    writes = [
        AgentEventWrite(
            event_type="agent.tool_called",
            causation_id=decision_id,
            payload=payload,
            occurred_at=occurred_at,
            event_id=called_event_id,
        )
    ]
    if task_id is not None:
        writes.append(
            AgentEventWrite(
                event_type="agent.task_state_changed",
                causation_id=called_event_id,
                payload={
                    "task_id": task_id,
                    "from_state": "pending",
                    "to_state": "running",
                    "reason": None,
                },
                occurred_at=occurred_at,
            )
        )
    await write_agent_events(
        session_factory,
        scope_key=scope_key,
        correlation_id=correlation_id,
        events=writes,
    )
    return EffectCallHandle(
        tool_call_id=tool_call_id,
        called_event_id=called_event_id,
        decision_id=decision_id,
        tool_name=tool_name,
        task_id=task_id,
        call_site=call_site,
        occurrence=occurrence,
    )


async def finish_effect_call(
    session_factory: SessionFactory,
    *,
    scope_key: str,
    correlation_id: str,
    handle: EffectCallHandle,
    outcome: ToolOutcome,
) -> str:
    """Transaction 2: generated domain events plus exactly one terminal."""
    writes = [
        AgentEventWrite(
            event_type=event.event_type,
            causation_id=handle.called_event_id,
            payload=event.payload,
            occurred_at=event.occurred_at,
        )
        for event in outcome.emitted_events
    ]
    writes.append(_tool_terminal_write(handle, outcome))
    event_ids = await write_agent_events(
        session_factory,
        scope_key=scope_key,
        correlation_id=correlation_id,
        events=writes,
    )
    return event_ids[-1]


def uncertain_outcome(
    *,
    error_kind: str,
    error_message: str,
    status: str = "uncertain",
    **extra: Any,
) -> ToolOutcome:
    return ToolOutcome.failure(
        error_kind,
        error_message,
        status=status,
        **extra,
    )


def _tool_terminal_write(
    handle: EffectCallHandle, outcome: ToolOutcome
) -> AgentEventWrite:
    common = {
        "tool_call_id": handle.tool_call_id,
        "tool_name": handle.tool_name,
        "task_id": handle.task_id,
    }
    if outcome.ok:
        return AgentEventWrite(
            event_type="agent.tool_result",
            causation_id=handle.called_event_id,
            payload={**common, "result": outcome.result},
        )
    payload = {
        **common,
        "error_kind": outcome.error_kind or "internal_tool_error",
        "error_message": outcome.error_message or "tool call failed",
    }
    if isinstance(outcome.extra, dict):
        payload.update(outcome.extra)
    return AgentEventWrite(
        event_type="agent.tool_failed",
        causation_id=handle.called_event_id,
        payload=payload,
    )


async def write_program_completed(  # noqa: PLR0913
    session_factory: SessionFactory,
    *,
    scope_key: str,
    correlation_id: str,
    decision_id: str,
    program_sha256: str,
    duration_ms: int,
    query_calls: list[str],
    effect_call_ids: list[str],
    result: Any,
    has_result: bool,
) -> str:
    return await write_agent_event(
        session_factory,
        event_type="agent.program_completed",
        scope_key=scope_key,
        correlation_id=correlation_id,
        causation_id=decision_id,
        payload={
            "decision_id": decision_id,
            "program_sha256": program_sha256,
            "duration_ms": duration_ms,
            "query_calls": query_calls,
            "effect_call_ids": effect_call_ids,
            "result": result,
            "has_result": has_result,
        },
    )


async def write_program_failed(  # noqa: PLR0913
    session_factory: SessionFactory,
    *,
    scope_key: str,
    correlation_id: str,
    decision_id: str,
    program_sha256: str,
    duration_ms: int,
    query_calls: list[str],
    effect_call_ids: list[str],
    error_kind: str,
    error_message: str,
    failed_call: dict[str, Any] | None = None,
    **details: Any,
) -> str:
    payload: dict[str, Any] = {
        "decision_id": decision_id,
        "program_sha256": program_sha256,
        "duration_ms": duration_ms,
        "query_calls": query_calls,
        "effect_call_ids": effect_call_ids,
        "error_kind": error_kind,
        "error_message": str(error_message)[:1000],
        "failed_call": failed_call,
    }
    payload.update({key: value for key, value in details.items() if value is not None})
    return await write_agent_event(
        session_factory,
        event_type="agent.program_failed",
        scope_key=scope_key,
        correlation_id=correlation_id,
        causation_id=decision_id,
        payload=payload,
    )


async def recover_interrupted_programs(
    session_factory: SessionFactory,
    *,
    scope_key: str,
) -> RecoveryReport:
    """Close every pre-existing half call/program in one scope; never replay."""
    rows = await _load_recovery_rows(session_factory, scope_key=scope_key)
    terminal_causes = {
        str(row.causation_id)
        for row in rows
        if row.type in _TOOL_TERMINALS | _PROGRAM_TERMINALS and row.causation_id
    }
    calls = [row for row in rows if row.type == "agent.tool_called"]
    decisions = [
        row
        for row in rows
        if row.type == "agent.decision_emitted"
        and isinstance(row.payload, dict)
        and "program" in row.payload
    ]

    tool_calls_closed = 0
    for row in calls:
        if str(row.event_id) in terminal_causes:
            continue
        payload = row.payload or {}
        await write_agent_event(
            session_factory,
            event_type="agent.tool_failed",
            scope_key=scope_key,
            correlation_id=str(row.correlation_id or new_event_id()),
            causation_id=str(row.event_id),
            payload={
                "tool_call_id": payload.get("tool_call_id"),
                "tool_name": payload.get("tool_name"),
                "task_id": payload.get("task_id"),
                "error_kind": "interrupted",
                "error_message": (
                    "process stopped before this effect produced a terminal; "
                    "delivery state is uncertain and the call was not replayed"
                ),
                "status": "uncertain",
            },
        )
        tool_calls_closed += 1

    programs_closed = 0
    calls_by_decision: dict[str, list[str]] = {}
    for row in calls:
        if row.causation_id:
            calls_by_decision.setdefault(str(row.causation_id), []).append(
                str((row.payload or {}).get("tool_call_id") or "")
            )
    for row in decisions:
        if str(row.event_id) in terminal_causes:
            continue
        payload = row.payload or {}
        await write_program_failed(
            session_factory,
            scope_key=scope_key,
            correlation_id=str(row.correlation_id or new_event_id()),
            decision_id=str(row.event_id),
            program_sha256=str(payload.get("program_sha256") or ""),
            duration_ms=0,
            query_calls=[],
            effect_call_ids=[
                call_id
                for call_id in calls_by_decision.get(str(row.event_id), [])
                if call_id
            ],
            error_kind="interrupted",
            error_message=(
                "process stopped before the program produced a terminal; "
                "the source was not replayed"
            ),
            status="uncertain",
        )
        programs_closed += 1

    return RecoveryReport(tool_calls_closed, programs_closed)


async def _load_recovery_rows(
    session_factory: SessionFactory, scope_key: str
) -> list[AgentEvent]:
    scope, group_id, user_id = parse_scope_key(scope_key)
    stmt = (
        select(AgentEvent)
        .where(AgentEvent.scope == scope)
        .where(AgentEvent.type.in_(_RECOVERY_TYPES))
    )
    if scope == "group":
        stmt = stmt.where(AgentEvent.group_id == group_id)
    elif scope == "private":
        stmt = stmt.where(AgentEvent.user_id == user_id)
    async with session_factory() as session:
        result = await session.execute(stmt)
        return list(result.scalars().all())


__all__ = [
    "EffectCallHandle",
    "RecoveryReport",
    "begin_effect_call",
    "finish_effect_call",
    "recover_interrupted_programs",
    "uncertain_outcome",
    "write_program_completed",
    "write_program_failed",
]

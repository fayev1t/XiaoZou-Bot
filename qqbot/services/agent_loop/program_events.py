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
# 收口只认工具调用（2026-08-17 提案-裁决流水线）：``agent.decision_emitted``
# 不再参与——决策事件和时间线上任何一条事件同级，写进去就是一段文本，没有
# 状态、没有生命周期、不需要收口。"这条执行过没有"永远是对 append-only 事件流
# 的一次查询（有没有以它为因的 program terminal），不是被维护的标志位。
_RECOVERY_TYPES = tuple({"agent.tool_called", *_TOOL_TERMINALS})
_PROGRAM_TERMINALS = frozenset({"agent.program_completed", "agent.program_failed"})
# 「这条决策已经开始执行」的判据（见 load_referenced_decision）。
_EXECUTION_MARKS = tuple({"agent.tool_called", *_PROGRAM_TERMINALS})


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


@dataclass(frozen=True)
class ReferencedDecision:
    """``execute_decision(event_id=…)`` 指名的那条历史决策。"""

    event_id: str
    correlation_id: str
    program: str
    program_sha256: str


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
    """Close every pre-existing half tool call in one scope; never replay.

    只收口 ``agent.tool_called``——那是真正发生过外部副作用、投递状态存疑的
    地方。决策事件不收口（见 ``_RECOVERY_TYPES`` 上的说明）；进程异常关闭后
    已开跑程序的整体收束由后续统一方案处理，不在这里。
    """
    rows = await _load_recovery_rows(session_factory, scope_key=scope_key)
    terminal_causes = {
        str(row.causation_id)
        for row in rows
        if row.type in _TOOL_TERMINALS and row.causation_id
    }
    calls = [row for row in rows if row.type == "agent.tool_called"]

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

    return RecoveryReport(tool_calls_closed)


async def load_referenced_decision(
    session_factory: SessionFactory,
    *,
    scope_key: str,
    event_id: str,
) -> tuple[ReferencedDecision | None, str | None]:
    """按事件 ID 取本 scope 的一条历史决策源码，附带"执行过没有"的查询结果。

    返回 ``(decision, error_kind)``——``error_kind`` 为 None 时 ``decision``
    必然非 None。两种拒绝：

    - ``decision_not_found``：本 scope 内没有这条事件，或它不是一条带源码的
      ``agent.decision_emitted``；
    - ``already_executed``：库里已经存在以它为因的 ``agent.tool_called`` 或
      program terminal。这是对 append-only 事件流的一次**查询**，不是被维护
      的状态位：决策事件本身没有任何字段记录自己执行过没有。

    为什么连 ``tool_called`` 也算数：program terminal 要等整段程序跑完才写，
    而并发派发下「已经在跑」的那几十秒里照样会开新拍——只看终态的话，模型
    能把同一段程序再裁决一次，副作用就出去两遍。``tool_called`` 按铁律**先于**
    副作用落库，所以「存在 tool_called」等价于「这段程序已经开始改变世界」，
    进程重启也不会让这个事实消失（半截调用由收口器写成 uncertain，从不重放）。
    """
    scope, group_id, user_id = parse_scope_key(scope_key)
    stmt = select(AgentEvent).where(AgentEvent.event_id == event_id)
    stmt = stmt.where(AgentEvent.scope == scope)
    if scope == "group":
        stmt = stmt.where(AgentEvent.group_id == group_id)
    elif scope == "private":
        stmt = stmt.where(AgentEvent.user_id == user_id)
    started_stmt = (
        select(AgentEvent.event_id)
        .where(AgentEvent.causation_id == event_id)
        .where(AgentEvent.type.in_(_EXECUTION_MARKS))
        .limit(1)
    )
    async with session_factory() as session:
        row = (await session.execute(stmt)).scalars().first()
        if row is None or row.type != "agent.decision_emitted":
            return None, "decision_not_found"
        payload = row.payload if isinstance(row.payload, dict) else {}
        program = payload.get("program")
        if not isinstance(program, str):
            return None, "decision_not_found"
        started = (await session.execute(started_stmt)).scalars().first()
    if started is not None:
        return None, "already_executed"
    return (
        ReferencedDecision(
            event_id=str(row.event_id),
            correlation_id=str(row.correlation_id or ""),
            program=program,
            program_sha256=str(payload.get("program_sha256") or ""),
        ),
        None,
    )


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
    "ReferencedDecision",
    "begin_effect_call",
    "finish_effect_call",
    "load_referenced_decision",
    "recover_interrupted_programs",
    "uncertain_outcome",
    "write_program_completed",
    "write_program_failed",
]

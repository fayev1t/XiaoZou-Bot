"""ReplyTask 的 append-only 写入与折叠。

ReplyTask 是 scope 内唯一、最长只存活几十秒的待发聚合，不使用读模型表。

2026-07-24（待办#19）起**内容不再合并**：每次 upsert 事件记录的是那一次调用
自己的授权原文（targets/gist/verbatim_messages），不是与历史的合并态。折叠出
的 ``ReplyTaskState`` 因此只回答"这份稿什么时候发、发到哪一步了"——调度字段
（flush_at/hard_deadline/state/revision）取最新一条 upsert，last-write-wins；
``targets``/``gist`` 字段留的是**最近一次授权原文**，不是有效授权集合。
完整授权序列在事件流里（也就是 timeline 上那几行 ``<tool-call name="reply">``），
由 flush 时的 Replyer 自行综合。cancel/claim/flush 把它推进到非 open 状态。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from qqbot.core.time import CHINA_TIMEZONE, china_now
from qqbot.models.agent_event import AgentEvent
from qqbot.services.agent_loop.event_writer import parse_scope_key, write_agent_event

SessionFactory = Callable[[], AsyncSession]

MAX_HOLD_SECONDS = 90
MAX_REPLY_EVENTS = 1000

_locks: dict[str, asyncio.Lock] = {}


def scope_lock(scope_key: str) -> asyncio.Lock:
    """单进程内串行化同 scope 的 reply_task 变更与 claim。"""
    lock = _locks.get(scope_key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[scope_key] = lock
    return lock


@dataclass(frozen=True)
class ReplyTaskState:
    reply_task_id: str
    scope_key: str
    revision: int
    state: str
    created_at: datetime
    updated_at: datetime
    flush_at: datetime
    hard_deadline: datetime
    mode: str
    targets: list[dict]
    gist: dict
    verbatim_messages: list[dict]
    latest_event_id: str
    source_tool_call_event_id: str | None
    correlation_id: str | None


async def load_open_reply_task(
    session_factory: SessionFactory, scope_key: str
) -> ReplyTaskState | None:
    tasks = await _load_scope_tasks(session_factory, scope_key)
    open_tasks = [task for task in tasks.values() if task.state == "open"]
    if not open_tasks:
        return None
    return max(open_tasks, key=lambda task: task.updated_at)


# load_reply_task(scope_key, reply_task_id) 已于 2026-07-24 删除（待办#19）：
# 它唯一的调用者是 cancel 的"按 id 精确取任务"路径，而 cancel 现在直接取
# scope 内那份 open task（至多一份，id 只作可选校验）。按 id 取任意状态的
# 任务在 append 语义下没有场景——claimed/flushed/cancelled 都不可再改。


async def load_open_reply_tasks(
    session_factory: SessionFactory,
) -> list[ReplyTaskState]:
    """启动 rescan：读取所有 scope 最近的 reply_task 事件并折叠 open 项。"""
    cutoff = china_now() - timedelta(hours=6)
    stmt = (
        select(AgentEvent)
        .where(AgentEvent.occurred_at >= cutoff)
        .where(AgentEvent.type.in_(_REPLY_EVENT_TYPES))
        .order_by(desc(AgentEvent.occurred_at), desc(AgentEvent.event_id))
        .limit(MAX_REPLY_EVENTS * 4)
    )
    async with session_factory() as session:
        rows = list((await session.execute(stmt)).scalars().all())
    tasks = _fold_rows(reversed(rows))
    return [task for task in tasks.values() if task.state == "open"]


async def load_recent_reply_tasks(
    session_factory: SessionFactory,
) -> list[ReplyTaskState]:
    cutoff = china_now() - timedelta(hours=6)
    stmt = (
        select(AgentEvent)
        .where(AgentEvent.occurred_at >= cutoff)
        .where(AgentEvent.type.in_(_REPLY_EVENT_TYPES))
        .order_by(desc(AgentEvent.occurred_at), desc(AgentEvent.event_id))
        .limit(MAX_REPLY_EVENTS * 4)
    )
    async with session_factory() as session:
        rows = list((await session.execute(stmt)).scalars().all())
    return list(_fold_rows(reversed(rows)).values())


async def find_upsert_for_tool_call(
    session_factory: SessionFactory,
    tool_call_event_id: str,
) -> dict | None:
    stmt = (
        select(AgentEvent)
        .where(AgentEvent.type == "agent.reply_task_upserted")
        .where(AgentEvent.causation_id == tool_call_event_id)
        .limit(1)
    )
    async with session_factory() as session:
        row = (await session.execute(stmt)).scalars().first()
    return dict(row.payload or {}) if row is not None else None


async def find_cancel_for_tool_call(
    session_factory: SessionFactory,
    tool_call_event_id: str,
) -> dict | None:
    """ToolWorker 终态落库前崩溃时，重放 cancel 仍返回原成功事实。"""
    stmt = (
        select(AgentEvent)
        .where(AgentEvent.type == "agent.reply_task_cancelled")
        .where(AgentEvent.causation_id == tool_call_event_id)
        .limit(1)
    )
    async with session_factory() as session:
        row = (await session.execute(stmt)).scalars().first()
    return dict(row.payload or {}) if row is not None else None


async def append_upsert(
    session_factory: SessionFactory,
    *,
    scope_key: str,
    correlation_id: str,
    tool_call_event_id: str,
    payload: dict,
) -> str:
    return await write_agent_event(
        session_factory,
        event_type="agent.reply_task_upserted",
        scope_key=scope_key,
        correlation_id=correlation_id,
        causation_id=tool_call_event_id,
        payload=payload,
    )


async def append_cancel(
    session_factory: SessionFactory,
    *,
    scope_key: str,
    correlation_id: str,
    tool_call_event_id: str,
    task: ReplyTaskState,
) -> str:
    return await write_agent_event(
        session_factory,
        event_type="agent.reply_task_cancelled",
        scope_key=scope_key,
        correlation_id=correlation_id,
        causation_id=tool_call_event_id,
        payload={
            "reply_task_id": task.reply_task_id,
            "revision": task.revision,
            "state": "cancelled",
        },
    )


def build_upsert_payload(
    *,
    reply_task_id: str,
    revision: int,
    created_at: datetime,
    updated_at: datetime,
    flush_at: datetime,
    hard_deadline: datetime,
    mode: str,
    targets: list[dict],
    gist: dict,
    verbatim_messages: list[dict],
) -> dict:
    return {
        "reply_task_id": reply_task_id,
        "revision": revision,
        "state": "open",
        "created_at": created_at.isoformat(),
        "updated_at": updated_at.isoformat(),
        "flush_at": flush_at.isoformat(),
        "hard_deadline": hard_deadline.isoformat(),
        "mode": mode,
        "targets": targets,
        "gist": gist,
        "verbatim_messages": verbatim_messages,
    }


# merge_targets / merge_gist / _dedupe_strings 已于 2026-07-24 删除（待办#19）。
# 它们做的是"把新授权并进旧授权"，而两者都只增不减：merge_targets 无法撤掉
# 一个已授权的 target，merge_gist 的 facts/avoid 是并集去重、写错的事实撤不
# 回来，模型只能往 avoid 里塞反向指令去抵消，让 gist 自相矛盾。改成 append-
# only 之后不存在"合并"这件事——每条授权原样入库，谁覆盖谁由 Replyer 按时间
# 顺序判断（最新的是权威）。


_REPLY_EVENT_TYPES = (
    "agent.reply_task_upserted",
    "agent.reply_task_cancelled",
    "runtime.reply_flush_claimed",
    "runtime.reply_flushed",
)


async def _load_scope_tasks(
    session_factory: SessionFactory, scope_key: str
) -> dict[str, ReplyTaskState]:
    scope, group_id, user_id = parse_scope_key(scope_key)
    cutoff = china_now() - timedelta(hours=6)
    stmt = (
        select(AgentEvent)
        .where(AgentEvent.type.in_(_REPLY_EVENT_TYPES))
        .where(AgentEvent.scope == scope)
        .where(AgentEvent.occurred_at >= cutoff)
        .order_by(desc(AgentEvent.occurred_at), desc(AgentEvent.event_id))
        .limit(MAX_REPLY_EVENTS)
    )
    if scope == "group":
        stmt = stmt.where(AgentEvent.group_id == group_id)
    elif scope == "private":
        stmt = stmt.where(AgentEvent.user_id == user_id)
    async with session_factory() as session:
        rows = list((await session.execute(stmt)).scalars().all())
    return _fold_rows(reversed(rows))


def _fold_rows(rows: Any) -> dict[str, ReplyTaskState]:
    tasks: dict[str, ReplyTaskState] = {}
    for row in rows:
        payload = dict(row.payload or {})
        task_id = str(payload.get("reply_task_id") or "")
        if not task_id:
            continue
        if row.type == "agent.reply_task_upserted":
            tasks[task_id] = ReplyTaskState(
                reply_task_id=task_id,
                scope_key=_scope_key(row),
                revision=int(payload.get("revision") or 1),
                state="open",
                created_at=_parse_dt(payload.get("created_at"), row.occurred_at),
                updated_at=_parse_dt(payload.get("updated_at"), row.occurred_at),
                flush_at=_parse_dt(payload.get("flush_at"), row.occurred_at),
                hard_deadline=_parse_dt(
                    payload.get("hard_deadline"), row.occurred_at
                ),
                mode=str(payload.get("mode") or "compose"),
                targets=list(payload.get("targets") or []),
                gist=dict(payload.get("gist") or {}),
                verbatim_messages=list(payload.get("verbatim_messages") or []),
                latest_event_id=row.event_id,
                source_tool_call_event_id=row.causation_id,
                correlation_id=row.correlation_id,
            )
            continue
        task = tasks.get(task_id)
        if task is None:
            continue
        state = {
            "agent.reply_task_cancelled": "cancelled",
            "runtime.reply_flush_claimed": "claimed",
            "runtime.reply_flushed": str(payload.get("status") or "sent"),
        }.get(row.type)
        if state:
            tasks[task_id] = ReplyTaskState(**{**task.__dict__, "state": state})
    return tasks


def _scope_key(row: Any) -> str:
    if row.scope == "group":
        return f"group:{row.group_id}"
    if row.scope == "private":
        return f"private:{row.user_id}"
    return "system"


def _parse_dt(raw: Any, fallback: datetime) -> datetime:
    if isinstance(raw, str):
        try:
            value = datetime.fromisoformat(raw)
            if value.tzinfo is None:
                value = value.replace(tzinfo=CHINA_TIMEZONE)
            return value.astimezone(CHINA_TIMEZONE)
        except ValueError:
            pass
    if fallback.tzinfo is not None:
        return fallback.astimezone(CHINA_TIMEZONE)
    return fallback.replace(tzinfo=CHINA_TIMEZONE)

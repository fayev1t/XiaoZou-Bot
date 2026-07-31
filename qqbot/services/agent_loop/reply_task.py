"""ReplyTask 的 append-only 写入与折叠。

ReplyTask 是 scope 内唯一、最长只存活几十秒的待发聚合，不使用读模型表。

2026-07-24（待办#19）起**内容不再合并**：每次 upsert 事件记录的是那一次调用
自己的授权原文（analysis），不是与历史的合并态。

2026-07-25 的补强把“完整、自足”落实为明确的 **latest-revision-wins**：
折叠出的 ``ReplyTaskState`` 同时携带最新一条 upsert 的调度字段与完整
``analysis``。旧 revision 仍留在 append-only 事件流供审计与 Planner 回看。

**2026-07-31 删除 Replyer 后的状态机**（重构提案-删除Replyer.md §1）：

    open ──flush_at 到达──→ completed        （runtime.reply_task_completed）
      └──reply(action="cancel")──→ cancelled  （agent.reply_task_cancelled）

只有这三个状态；completed / cancelled 都是 terminal。到点由 ReplyExecutor
写一条 ``runtime.reply_task_completed``（携带完整 analysis）并立即唤醒
Planner——发不发、发什么由 Planner 那一拍自己决定（``send_messages`` 工具），
ReplyTask 的生命周期到完成事件为止。新链路**不再写** ``runtime.reply_flush_
claimed`` / ``reply_flushed``（发送事实活在 ``send_messages`` 的 tool
terminal 里）；升级前的旧 claim/flush 事件仍被识别，只用于把升级前的任务
折成历史 terminal。

折叠对过期完成事件的拒绝规则（§1.5，写入侧之外的第二道防线）：更低
revision 的 completed 不折（已有更新的 upsert 在场）；已 cancelled 的任务
不被迟到的 completed 复活。

2026-07-30 删除 ``mode`` 与 ``verbatim_messages``：逐字直发（``action=
"verbatim"``）整条下线后留下的两个键在折叠时被直接忽略；旧 verbatim 事件的
``analysis`` 恒为空串。
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
    analysis: str
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


async def find_completed_for_upsert(
    session_factory: SessionFactory,
    upsert_event_id: str,
) -> str | None:
    """按去重键（type + causation_id=该 revision 的 upsert）查既有完成事件。

    并发定时器回调 / 重启 rescan 重入时，同一个最新 revision 只允许产生一条
    ``runtime.reply_task_completed``；已存在则返回既有 event_id，调用方不再写。
    """
    stmt = (
        select(AgentEvent.event_id)
        .where(AgentEvent.type == "runtime.reply_task_completed")
        .where(AgentEvent.causation_id == upsert_event_id)
        .limit(1)
    )
    async with session_factory() as session:
        row = (await session.execute(stmt)).scalars().first()
    return row


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
    analysis: str,
) -> dict:
    """一次追加的领域事件 payload。

    ``analysis`` 是 Planner 对局势的解析备忘（谁对谁说话、话题线、决定性时
    序、待解决内容、已核实事实与存疑处），**不是**最终可见文案。每次事件原样
    留档；折叠态只取最新 revision 的完整 analysis，到点后随
    ``runtime.reply_task_completed`` 自包含地回到时间线——Planner 不必从旧
    tool-call 行里猜哪次 revision 才是当前事实。
    """
    return {
        "reply_task_id": reply_task_id,
        "revision": revision,
        "state": "open",
        "created_at": created_at.isoformat(),
        "updated_at": updated_at.isoformat(),
        "flush_at": flush_at.isoformat(),
        "hard_deadline": hard_deadline.isoformat(),
        "analysis": analysis,
    }


# merge_targets / merge_gist / _dedupe_strings 已于 2026-07-24 删除（待办#19）。
# 它们做的是"把新授权并进旧授权"，而两者都只增不减：merge_targets 无法撤掉
# 一个已授权的 target，merge_gist 的 facts/avoid 是并集去重、写错的事实撤不
# 回来，模型只能往 avoid 里塞反向指令去抵消，让 gist 自相矛盾。改成 append-
# only 之后不存在"合并"这件事——每条授权原样入库，折叠态只取最新 revision
# 的完整 analysis；旧 revision 不再作为补丁参与组稿。targets/gist 本身随后于
# 2026-07-25 被单个自由文本字段取代；2026-07-28 该字段进一步明确为 analysis，
# 专门承载人物/话题/时序与事实判读，省略旧内容即撤回旧内容。


_REPLY_EVENT_TYPES = (
    "agent.reply_task_upserted",
    "agent.reply_task_cancelled",
    "runtime.reply_task_completed",
    # 升级兼容：旧链路的 claim/flush 只用于把升级前的任务折成历史 terminal，
    # 新链路不再写它们（发送事实活在 send_messages 的 tool terminal 里）。
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
            # 2026-07-28 工具与领域事件改名为 analysis。升级前已经持久化的事件
            # 仍使用 brief；只在新键完全不存在时读取旧键，避免损坏的新事件被旧值
            # 悄悄掩盖。
            if "analysis" in payload:
                raw_analysis = payload.get("analysis")
            else:
                raw_analysis = payload.get("brief")
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
                # 领域事件正常只可能来自 ReplyTool 的严格校验；若旧库/损坏事件
                # 仍塞入非字符串，折成空 analysis——绝不能把 list/dict 的 repr
                # 当成一份对话解析。旧 verbatim 事件的 analysis 恒为空串
                # （2026-07-30）。
                analysis=raw_analysis if isinstance(raw_analysis, str) else "",
                latest_event_id=row.event_id,
                source_tool_call_event_id=row.causation_id,
                correlation_id=row.correlation_id,
            )
            continue
        task = tasks.get(task_id)
        if task is None:
            continue
        if row.type == "runtime.reply_task_completed":
            # 过期完成事件拒绝（§1.5）：更低 revision 的 completed 说明它输给
            # 了并发追加的新 upsert，不折；cancelled 是 terminal，迟到的
            # completed 不复活它。
            revision = payload.get("revision")
            if isinstance(revision, int) and revision != task.revision:
                continue
            if task.state == "cancelled":
                continue
            tasks[task_id] = ReplyTaskState(
                **{**task.__dict__, "state": "completed"}
            )
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

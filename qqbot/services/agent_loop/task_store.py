"""task_store —— agent_tasks 读模型的写入 / 读取 / 回填。

定位见 models/agent_task.py 顶部注释。三个对外接口：

  apply_task_event(session, ...)   底层投影：把一条 agent.task_* 事件 upsert 进
                                   agent_tasks。**不 commit**（commit 由调用方
                                   控制），供 backfill 批量复用同一 session。
  apply_task_event_safe(factory…)  live 写路径用：独立事务包一层 apply_task_event
                                   + commit + 吞异常。事件落定后才调（见下）。

  load_active_tasks(factory, key)  读：取某 scope 下所有 pending/running 任务，
                                   还原成 TaskView。Projector 用它把"窗口外仍
                                   未完成"的任务补回 active_tasks。

  backfill_recent(factory, ...)    回填：启动时 replay 最近 N 天的 task 事件，
                                   覆盖"首次部署本特性"和"读模型疑似漂移"。

设计取舍：
- progress_notes 用 JSONB `||` 追加（无 read-modify-write，天然原子），不在写侧
  截断；读侧 _row_to_task_view 取尾部 N 条。极长寿任务的 notes 数组会增长，但
  单条很小且读侧封顶，可接受；真·卡死任务交给驱逐机制（⑥§5.2）。
- pending_tool_call_ids 读模型不维护 —— 在途调用是近期事件，Projector 窗口折叠
  负责；表里一律给空列表。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable

from sqlalchemy import cast, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from qqbot.core.logging import get_logger
from qqbot.core.time import CHINA_TIMEZONE, china_now
from qqbot.models.agent_event import AgentEvent
from qqbot.models.agent_task import AgentTask
from qqbot.services.agent_loop.decision import ProgressNote, TaskView

logger = get_logger(__name__)

SessionFactory = Callable[[], AsyncSession]

# 与 Projector.MAX_PROGRESS_NOTES_PER_TASK 对齐，避免两条读路径渲染不一致。
MAX_PROGRESS_NOTES = 5

_TASK_EVENT_TYPES = (
    "agent.task_created",
    "agent.task_state_changed",
    "agent.task_progress_noted",
)


async def apply_task_event(
    session: AsyncSession,
    *,
    event_type: str,
    scope_key: str,
    occurred_at: datetime,
    payload: dict,
) -> None:
    """把一条 agent.task_* 事件投影进 agent_tasks（不 commit）。

    在调用方的事务里执行、由调用方负责 commit（live 路径走 apply_task_event_safe
    独立事务；backfill 走批量同事务）。未知 task_* 类型、缺 task_id / to_state /
    note 时静默 no-op（与 fold_tasks 的宽容语义一致）。
    """
    task_id = payload.get("task_id")
    if not task_id:
        return

    if event_type == "agent.task_created":
        stmt = (
            pg_insert(AgentTask)
            .values(
                task_id=task_id,
                scope_key=scope_key,
                description=payload.get("description") or "",
                related_tools=list(payload.get("related_tools") or []),
                parent_task_id=payload.get("parent_task_id"),
                state="pending",
                created_at=occurred_at,
                last_changed_at=occurred_at,
                last_change_reason=None,
                triggered_by_event_id=payload.get("triggered_by_event_id"),
                progress_notes=[],
            )
            # 幂等：replay / 回填重复见到同一 task_created 时，只刷新描述类元数据，
            # **不**重置 state / created_at / progress_notes（这些已被后续事件推进）。
            .on_conflict_do_update(
                index_elements=["task_id"],
                set_={
                    "scope_key": scope_key,
                    "description": payload.get("description") or "",
                    "related_tools": list(payload.get("related_tools") or []),
                    "parent_task_id": payload.get("parent_task_id"),
                    "triggered_by_event_id": payload.get("triggered_by_event_id"),
                },
            )
        )
        await session.execute(stmt)

    elif event_type == "agent.task_state_changed":
        to_state = payload.get("to_state")
        if not to_state:
            return
        # 无条件 set 到 to_state（与 fold_tasks 一致：事件按时间序到达，最后一条
        # 胜）。row 不存在（任务早于建表、且未被回填）时 UPDATE 影响 0 行，静默。
        stmt = (
            update(AgentTask)
            .where(AgentTask.task_id == task_id)
            .values(
                state=to_state,
                last_changed_at=occurred_at,
                last_change_reason=payload.get("reason"),
            )
        )
        await session.execute(stmt)

    elif event_type == "agent.task_progress_noted":
        note = payload.get("note")
        if not note:
            return
        new_note = [{"at": occurred_at.isoformat(), "note": str(note)}]
        # JSONB `||` 追加，原子无竞态。row 不存在时影响 0 行，静默。
        stmt = (
            update(AgentTask)
            .where(AgentTask.task_id == task_id)
            .values(
                progress_notes=AgentTask.progress_notes.op("||")(
                    cast(new_note, JSONB)
                ),
            )
        )
        await session.execute(stmt)

    # 其它 agent.task_* 类型：no-op（前向兼容）


async def apply_task_event_safe(
    session_factory: SessionFactory,
    *,
    event_type: str,
    scope_key: str,
    occurred_at: datetime,
    payload: dict,
) -> None:
    """live 写路径用：独立事务 upsert 读模型，吞掉所有异常（只 log）。

    关键：读模型是 agent_events 的**派生视图**，它的失败绝不能反过来拖垮事件
    append —— append-only 事件流的持久性是最高优先级（CLAUDE.md 硬规矩）。因此
    这里**不与事件写同事务**（同事务下 upsert 失败会 abort 整个 PG 事务，连带
    丢掉本该落定的 task 事件）。漂移由 `backfill_recent`（启动回填）+ Projector
    混合折叠（窗口内仍走 fold_tasks，刚写的任务必在窗口里）兜底自愈。
    """
    try:
        async with session_factory() as session:
            await apply_task_event(
                session,
                event_type=event_type,
                scope_key=scope_key,
                occurred_at=occurred_at,
                payload=payload,
            )
            await session.commit()
    except Exception as exc:
        logger.warning(
            "[task_store] read-model upsert failed for {} (event persisted, "
            "will self-heal via backfill): {}",
            payload.get("task_id"),
            exc,
        )


async def load_active_tasks(
    session_factory: SessionFactory, scope_key: str
) -> list[TaskView]:
    """取某 scope 下所有 pending/running 任务，按创建时间升序还原成 TaskView。

    不受 lookback / 条数窗口限制 —— 这正是它能修"未完成任务被水群挤掉"的原因。
    """
    stmt = (
        select(AgentTask)
        .where(AgentTask.scope_key == scope_key)
        .where(AgentTask.state.in_(("pending", "running")))
        .order_by(AgentTask.created_at.asc())
    )
    async with session_factory() as session:
        result = await session.execute(stmt)
        rows = list(result.scalars().all())
    return [_row_to_task_view(r) for r in rows]


async def backfill_recent(
    session_factory: SessionFactory, *, days: int = 30
) -> int:
    """启动时把最近 ``days`` 天的 agent.task_* 事件 replay 进读模型，幂等。

    覆盖两种场景：
    - 表刚建（首次部署本特性）：把现存未完成任务灌进来，否则它们要等下一次
      task 事件才进表。
    - 读模型疑似漂移：replay 用 upsert / UPDATE 修正。

    只回放 task 事件（量远小于消息），且只回放 ``days`` 天内 —— 开了 30 天还没
    结的任务属于卡死，交给驱逐机制（⑥§5.2），不在这里兜。返回回放的事件条数。
    """
    cutoff = china_now() - timedelta(days=days)
    stmt = (
        select(AgentEvent)
        .where(AgentEvent.type.in_(_TASK_EVENT_TYPES))
        .where(AgentEvent.occurred_at >= cutoff)
        .order_by(AgentEvent.occurred_at.asc())
    )
    async with session_factory() as session:
        result = await session.execute(stmt)
        rows = list(result.scalars().all())
        for row in rows:
            await apply_task_event(
                session,
                event_type=row.type,
                scope_key=_scope_key_from_event(row),
                occurred_at=row.occurred_at,
                payload=dict(row.payload or {}),
            )
        await session.commit()
    return len(rows)


# ─── helpers ───


def _row_to_task_view(row: Any) -> TaskView:
    """AgentTask ORM row → TaskView。progress_notes 取尾部 N 条并解析时间。"""
    notes_raw = list(row.progress_notes or [])[-MAX_PROGRESS_NOTES:]
    progress: list[ProgressNote] = []
    for n in notes_raw:
        if not isinstance(n, dict):
            continue
        at = _parse_iso(n.get("at"))
        note = n.get("note")
        if at is not None and note:
            progress.append(ProgressNote(at=at, note=str(note)))
    return TaskView(
        task_id=row.task_id,
        scope_key=row.scope_key,
        description=row.description or "",
        related_tools=list(row.related_tools or []),
        parent_task_id=row.parent_task_id,
        state=row.state,  # type: ignore[arg-type]
        created_at=_norm_china(row.created_at),
        last_changed_at=_norm_china(row.last_changed_at),
        last_change_reason=row.last_change_reason,
        # 在途工具调用不在读模型维护，交给 Projector 窗口折叠（见模块顶部注释）。
        pending_tool_call_ids=[],
        triggered_by_event_id=row.triggered_by_event_id,
        progress_notes=progress,
    )


def _norm_china(dt: datetime | None) -> datetime:
    """asyncpg 把 TIMESTAMPTZ 读回 UTC tzinfo；统一 normalize 到北京时间，
    与 projection._snapshot_from_row 保持一致（否则 TaskView 时间尾巴会和
    timeline 不一致）。"""
    if dt is not None and dt.tzinfo is not None:
        return dt.astimezone(CHINA_TIMEZONE)
    return dt  # type: ignore[return-value]


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(CHINA_TIMEZONE)
    return dt


def _scope_key_from_event(row: Any) -> str:
    """AgentEvent 行的 (scope, group_id, user_id) → scope_key。"""
    if row.scope == "group" and row.group_id is not None:
        return f"group:{row.group_id}"
    if row.scope == "private" and row.user_id is not None:
        return f"private:{row.user_id}"
    return "system"

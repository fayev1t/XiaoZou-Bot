"""Internal event writers for agent.* and runtime.* events.

External events go through EventIngest (with idempotency_key + ON CONFLICT).
Internal events (agent.* / runtime.*) come from the loop and its runtime
workers; no external dedup is needed because they have unique event_ids
generated locally.

``announce()`` is the single "写一条事实，然后叫醒这个 scope" boundary
(2026-08-04)。此前 wait 工具、SilenceWatcher、ReplyExecutor 各写一遍
persist-then-notify，连"写失败还叫不叫醒"这种真语义都埋在各自的 try 里；
现在只有这一个函数，差异全部收敛成参数。``RuntimeEventPublisher``
退化成一个绑好配置的薄封装，供 ReplyExecutor 这种要长期持有 publisher 的
生产者使用。

公开唤醒一律进 AgentLoop 的固定攒批窗口；静默计时器武装不再挂在 wake 上，
而由 ``note_activity`` 在成功写入非 ``runtime.silence_elapsed`` 的
agent_visible 事实后触发（见 announce）。本模块看到的 wake 永远是朴素的
``(scope_key) -> Awaitable[None]``。

Contract: 开发文档/v2.0/事件系统设计.md §2, §4.2-4.3
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from qqbot.core.ids import new_event_id
from qqbot.core.logging import get_logger
from qqbot.core.time import china_now
from qqbot.services.event_ingest.persistence import persist_event
from qqbot.services.event_ingest.system_event import SystemEvent

logger = get_logger(__name__)

# 与 silence_watcher.SILENCE_EVENT_TYPE 同值；此处内联避免环形 import。
# 静默事实本身不算"有动静"，写成功后不得 note_activity。
_SILENCE_ELAPSED_TYPE = "runtime.silence_elapsed"

SessionFactory = Callable[[], AsyncSession]
EventAvailableNotifier = Callable[[str], Awaitable[None]]
ActivityNotifier = Callable[[str], None]
RuntimeEventWriter = Callable[..., Awaitable[str]]


@dataclass(frozen=True)
class AgentEventWrite:
    """一条待写入的 agent.* 事件。

    ``event_id`` 可由调用方预先指定（inline 调度需要先把 tool_called id 注入
    工具 context）；省略时 batch writer 按列表顺序生成，确保同时间戳事件仍按
    ULID 保持因果顺序。
    """

    event_type: str
    causation_id: str | None
    payload: dict
    occurred_at: datetime | None = None
    event_id: str | None = None


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
    sys_event = _build_system_event(
        event_id=new_event_id(),
        occurred_at=occurred_at or china_now(),
        origin=origin,
        event_type=event_type,
        scope=scope,
        group_id=group_id,
        user_id=user_id,
        visibility=visibility,
        correlation_id=correlation_id,
        causation_id=causation_id,
        payload=payload,
    )
    async with session_factory() as session:
        await persist_event(session, sys_event)
    await _project_task_event(session_factory, scope_key, sys_event)
    return sys_event.event_id


async def write_agent_events(
    session_factory: SessionFactory,
    *,
    scope_key: str,
    correlation_id: str,
    events: list[AgentEventWrite],
) -> list[str]:
    """原子追加一组 agent.* 事件，并按输入顺序返回 event_id。

    组内所有 INSERT 使用同一个 session、最后只 commit 一次；任何一条失败都不
    会留下半截 inline 调用链。派生的 ``agent_tasks`` 读模型仍在主事务提交后
    best-effort 更新，不能反向拖垮 append-only 事实流。
    """
    if not events:
        return []
    scope, group_id, user_id = parse_scope_key(scope_key)
    default_occurred_at = china_now()
    system_events: list[SystemEvent] = []
    for event in events:
        if not event.event_type.startswith("agent."):
            raise ValueError(
                "write_agent_events only accepts agent.* events; "
                f"got {event.event_type!r}"
            )
        system_events.append(
            _build_system_event(
                event_id=event.event_id or new_event_id(),
                occurred_at=event.occurred_at or default_occurred_at,
                origin="agent",
                event_type=event.event_type,
                scope=scope,
                group_id=group_id,
                user_id=user_id,
                visibility="agent_visible",
                correlation_id=correlation_id,
                causation_id=event.causation_id,
                payload=event.payload,
            )
        )

    async with session_factory() as session:
        for system_event in system_events:
            await persist_event(session, system_event, commit=False)
        await session.commit()

    for system_event in system_events:
        await _project_task_event(session_factory, scope_key, system_event)
    return [event.event_id for event in system_events]


def _build_system_event(  # noqa: PLR0913
    *,
    event_id: str,
    occurred_at: datetime,
    origin: str,
    event_type: str,
    scope: str,
    group_id: int | None,
    user_id: int | None,
    visibility: str,
    correlation_id: str,
    causation_id: str | None,
    payload: dict,
) -> SystemEvent:
    return SystemEvent(
        event_id=event_id,
        occurred_at=occurred_at,
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


async def _project_task_event(
    session_factory: SessionFactory,
    scope_key: str,
    event: SystemEvent,
) -> None:
    # 读模型双写：事件落定**之后**，独立事务 best-effort 投影进 agent_tasks。
    # 刻意不与事件写同事务 —— 派生视图的失败不能拖垮 append-only 事件流的持久性。
    if not event.type.startswith("agent.task_"):
        return
    from qqbot.services.agent_loop.task_store import apply_task_event_safe

    await apply_task_event_safe(
        session_factory,
        event_type=event.type,
        scope_key=scope_key,
        occurred_at=event.occurred_at,
        payload=event.payload,
    )


async def write_runtime_event(
    session_factory: SessionFactory,
    *,
    event_type: str,
    scope_key: str,
    visibility: str,
    correlation_id: str,
    causation_id: str | None,
    payload: dict,
    occurred_at: datetime | None = None,
) -> str:
    """``occurred_at`` 缺省=写入时刻；只有"事件真正发生的时刻早于写入"的
    场景才显式回填（如 runtime.context_compacted 回填为覆盖边界 +1ms，
    见 记忆系统契约 §2.2）。"""
    return await write_internal_event(
        session_factory,
        origin="runtime",
        event_type=event_type,
        scope_key=scope_key,
        visibility=visibility,
        correlation_id=correlation_id,
        causation_id=causation_id,
        payload=payload,
        occurred_at=occurred_at,
    )


async def announce(  # noqa: PLR0913
    session_factory: SessionFactory,
    *,
    event_type: str,
    scope_key: str,
    visibility: str,
    correlation_id: str,
    causation_id: str | None,
    payload: dict,
    wake: EventAvailableNotifier | None = None,
    note_activity: ActivityNotifier | None = None,
    wake_on_write_failure: bool = False,
    occurred_at: datetime | None = None,
    write_event: RuntimeEventWriter | None = None,
) -> str | None:
    """写一条 runtime 事实，落库后叫醒该 scope。**唯一**的 persist-then-notify 入口。

    顺序不可颠倒（事件系统设计.md §2）：wake 不能领先于事实，否则被叫醒那一拍
    的投影读不到自己被叫醒的理由。

    ``wake`` 是朴素的 ``(scope_key) -> Awaitable[None]``，一律进攒批窗口。传
    None = 只写不叫。

    ``note_activity`` 在**写成功**且事件为 agent_visible、且不是静默事实时同步
    调用，用来重排静默计时器。静默叫醒自己写的 ``runtime.silence_elapsed`` 不
    算动静（一段静默只响一次）。写失败不 note——没有新事实可算活动。

    ``wake_on_write_failure``：

    - ``False``（reply 完成）：写失败就不叫。
    - ``True``（``wait`` / 静默到点）：写失败仍然叫醒。

    通知本身永远 best-effort。返回 event_id；写失败且仍叫醒时返回 None。
    """
    writer = write_event or write_runtime_event
    event_id: str | None = None
    try:
        event_id = await writer(
            session_factory,
            event_type=event_type,
            scope_key=scope_key,
            visibility=visibility,
            correlation_id=correlation_id,
            causation_id=causation_id,
            payload=payload,
            occurred_at=occurred_at,
        )
    except Exception as exc:
        if not wake_on_write_failure:
            raise
        logger.warning(
            "[announce] write {} for {} failed (still waking): {}",
            event_type,
            scope_key,
            exc,
        )

    if (
        event_id is not None
        and visibility == "agent_visible"
        and event_type != _SILENCE_ELAPSED_TYPE
        and note_activity is not None
    ):
        try:
            note_activity(scope_key)
        except Exception as exc:  # noqa: BLE001 — 武装失败不拖垮主路径
            logger.warning(
                "[announce] note_activity {} after {} failed: {}",
                scope_key,
                event_type,
                exc,
            )

    if wake is None:
        return event_id
    # runtime_only 事实不进模型视野，为它开一拍没有意义。写失败时不走这条：
    # 那种情况下叫醒是先前那句约定的兑现，与写成了什么无关。
    if event_id is not None and visibility != "agent_visible":
        return event_id
    try:
        await wake(scope_key)
    except Exception as exc:  # noqa: BLE001 — event 已落库，通知必须 best-effort
        logger.warning(
            "[announce] wake {} after {} failed: {}", scope_key, event_type, exc
        )
    return event_id


class RuntimeEventPublisher:
    """``announce()`` 的绑定配置封装，供需要长期持有发布口的生产者使用。

    ReplyExecutor 在构造时拿到它、之后只管 ``publish(...)``，不必每次重复
    传 wake / note_activity 与失败策略。语义完全等同于直接调 ``announce()``。
    """

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        notify_event_available: EventAvailableNotifier | None = None,
        note_activity: ActivityNotifier | None = None,
        write_event: RuntimeEventWriter | None = None,
        wake_on_write_failure: bool = False,
    ) -> None:
        self._session_factory = session_factory
        self._notify_event_available = notify_event_available
        self._note_activity = note_activity
        self._write_event = write_event
        self._wake_on_write_failure = wake_on_write_failure

    async def publish(  # noqa: PLR0913
        self,
        *,
        event_type: str,
        scope_key: str,
        visibility: str,
        correlation_id: str,
        causation_id: str | None,
        payload: dict,
        occurred_at: datetime | None = None,
    ) -> str | None:
        return await announce(
            self._session_factory,
            event_type=event_type,
            scope_key=scope_key,
            visibility=visibility,
            correlation_id=correlation_id,
            causation_id=causation_id,
            payload=payload,
            wake=self._notify_event_available,
            note_activity=self._note_activity,
            wake_on_write_failure=self._wake_on_write_failure,
            occurred_at=occurred_at,
            write_event=self._write_event,
        )


async def write_agent_event(
    session_factory: SessionFactory,
    *,
    event_type: str,
    scope_key: str,
    correlation_id: str,
    causation_id: str | None,
    payload: dict,
    occurred_at: datetime | None = None,
) -> str:
    """``occurred_at`` 缺省=写入时刻。只有"事件真正发生的时刻早于写入"的
    场景才显式传（如 agent.decision_emitted 回填为本拍投影时刻，见
    loop._tick）——事件流按 occurred_at 排序，用错基准会让时间线错位。"""
    event_ids = await write_agent_events(
        session_factory,
        scope_key=scope_key,
        correlation_id=correlation_id,
        events=[
            AgentEventWrite(
                event_type=event_type,
                causation_id=causation_id,
                payload=payload,
                occurred_at=occurred_at,
            )
        ],
    )
    return event_ids[0]

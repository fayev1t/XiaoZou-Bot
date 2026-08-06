"""ReplyTask 到点后的生命周期完成执行器。

2026-07-31 删除 Replyer（重构提案-删除Replyer.md）：到点不再组稿、不再碰
OneBot。执行器只做一件事——在 scope 锁内复核最新 revision 仍 open 且已到期，
append 一条 ``runtime.reply_task_completed``（once，去重键 = 该 revision 的
upsert event_id），提交成功后经注入的 publisher 通知唤醒 Planner（统一 3s
攒批窗口；publisher 协议本身不变，§3.1）。发不发、发什么由 Planner 醒来那一拍结合最新
时间流自己决定（``send_messages`` 工具）；完成事件只表达"等待阶段结束了"，
**不是**发言授权，也没有消费/TTL 概念（§0.4）。

2026-08-01 删除 analysis 后完成事件不再携带任何内容，这条链路上执行器要搬运
的东西也就只剩 reply_task_id/revision 与两个时刻——它是一次纯粹的到点通知。

通知沿用现网 best-effort 语义（§3.2）：notifier 失败只记日志，不建消费审计
或 rescan outbox；极端 commit/notify 缝隙由后续任一正常 scope wake 兜底
（完成事件仍在窗口内即会被投影读到）。

启动 rescan 两件事：
- 重挂 open 任务的定时器；已过 flush_at 的立即触发，照常走完成路径（完成
  没有聊天副作用，重启后直接补上是安全的——旧链路的 reply_task_overdue
  提示事件随组稿路径一并退役）；
- 升级期旧 flush 恢复：旧链路 claimed 态 / 已有 durable ``reply_flush``
  claim 的任务补一条 uncertain flushed（保留一个版本周期后删除）。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from qqbot.core.ids import new_event_id
from qqbot.core.logging import get_logger
from qqbot.core.time import china_now
from qqbot.services.agent_loop.delivery_claims import has_delivery_claim
from qqbot.services.agent_loop.event_writer import RuntimeEventPublisher
from qqbot.services.agent_loop.reply_task import (
    ReplyTaskState,
    find_completed_for_upsert,
    load_open_reply_task,
    load_open_reply_tasks,
    load_recent_reply_tasks,
    scope_lock,
)

logger = get_logger(__name__)


class ReplyExecutor:
    def __init__(
        self,
        *,
        session_factory: Any,
        event_publisher: RuntimeEventPublisher | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._events = event_publisher or RuntimeEventPublisher(session_factory)
        self._handles: dict[str, asyncio.TimerHandle] = {}
        self._running: set[asyncio.Task[None]] = set()
        self._stopped = False

    async def start(self) -> None:
        self._stopped = False
        recent = await load_recent_reply_tasks(self._session_factory)
        recovered: set[str] = set()
        # ── 升级期旧 flush 恢复（§3.4）：旧链路"已 claim、尚未 final"的任务
        # 不能静默消失，补 uncertain。新链路的 open 任务只走 completed，不再
        # claim reply_flush，这段命中不了它们。
        for task in recent:
            claimed_without_event = task.state == "open" and await has_delivery_claim(
                self._session_factory, task.latest_event_id, "reply_flush"
            )
            if task.state != "claimed" and not claimed_without_event:
                continue
            reason = "process restarted after flush claim; not retrying"
            if claimed_without_event:
                reason = (
                    "process restarted after durable claim but before claim event; "
                    "not retrying"
                )
            await self._write_uncertain_recovery(task, reason)
            recovered.add(task.reply_task_id)
        # ── open 任务重挂定时器：已过期的 delay=0 立即触发，走正常完成路径。
        for task in (
            task
            for task in recent
            if task.state == "open" and task.reply_task_id not in recovered
        ):
            self._schedule(task.reply_task_id, task.revision, task.flush_at)

    async def stop(self) -> None:
        self._stopped = True
        for handle in self._handles.values():
            handle.cancel()
        self._handles.clear()
        running = list(self._running)
        for task in running:
            task.cancel()
        if running:
            await asyncio.gather(*running, return_exceptions=True)
        self._running.clear()

    async def notify(
        self,
        scope_key: str,
        reply_task_id: str,
        revision: int,
        flush_at: datetime,
        event_id: str,
    ) -> None:
        del scope_key, event_id
        self._schedule(reply_task_id, revision, flush_at)

    def _schedule(
        self, reply_task_id: str, revision: int, flush_at: datetime
    ) -> None:
        if self._stopped:
            return
        old = self._handles.pop(reply_task_id, None)
        if old is not None:
            old.cancel()
        delay = max((flush_at - china_now()).total_seconds(), 0.0)
        loop = asyncio.get_running_loop()
        self._handles[reply_task_id] = loop.call_later(
            delay, self._launch, reply_task_id, revision
        )

    def _launch(self, reply_task_id: str, revision: int) -> None:
        if self._stopped:
            return
        task = asyncio.create_task(
            self._fire(reply_task_id, revision),
            name=f"reply_complete:{reply_task_id}:{revision}",
        )
        self._running.add(task)
        task.add_done_callback(self._fire_done)

    def _fire_done(self, task: asyncio.Task[None]) -> None:
        self._running.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error("[reply_executor] complete task crashed: {}", error)

    async def _fire(self, reply_task_id: str, revision: int) -> None:
        self._handles.pop(reply_task_id, None)
        task = await self._find_open(reply_task_id)
        if task is None:
            return
        # 进程内 scope 锁串行化同 scope 的 upsert/cancel/complete。DB 级
        # advisory 锁（多实例串行）属于推迟的底层加固，当前部署为单实例。
        async with scope_lock(task.scope_key):
            current = await load_open_reply_task(
                self._session_factory, task.scope_key
            )
            if current is None or current.reply_task_id != reply_task_id:
                return
            if current.revision != revision:
                self._schedule(
                    current.reply_task_id, current.revision, current.flush_at
                )
                return
            if current.flush_at > china_now():
                self._schedule(
                    current.reply_task_id, current.revision, current.flush_at
                )
                return
            # once：同一最新 revision 只产生一条完成事件（并发回调 / rescan
            # 重入时读到既有事件即返回，不再写）。
            existing = await find_completed_for_upsert(
                self._session_factory, current.latest_event_id
            )
            if existing is not None:
                return
            await self._write_completed(current)

    async def _find_open(self, reply_task_id: str) -> ReplyTaskState | None:
        for task in await load_open_reply_tasks(self._session_factory):
            if task.reply_task_id == reply_task_id:
                return task
        return None

    async def _write_completed(self, task: ReplyTaskState) -> None:
        """append 完成事件，persist-then-notify（§3.1）。

        payload **只有调度事实**：2026-08-01 删除 analysis 后，这条事件表达的
        全部意思就是"这段等待结束了"。它不带内容、也不带授权 ID、可用次数、
        消费状态或 TTL（§0.4/§1.3）——醒来那一拍该说什么，由那时的时间线决
        定。publisher 先 commit 再调注入的 notifier（supervisor 的
        统一攒批 wake），wake 到达时投影必然读得到完成事件；通知失败
        不回滚事件（best-effort，后续自然 wake 兜底）。
        """
        correlation_id = task.correlation_id or new_event_id()
        await self._events.publish(
            event_type="runtime.reply_task_completed",
            scope_key=task.scope_key,
            visibility="agent_visible",
            correlation_id=correlation_id,
            causation_id=task.latest_event_id,
            payload={
                "reply_task_id": task.reply_task_id,
                "revision": task.revision,
                "flush_at": task.flush_at.isoformat(),
                "hard_deadline": task.hard_deadline.isoformat(),
                "completed_at": china_now().isoformat(),
                "source_tool_call_event_id": task.source_tool_call_event_id,
            },
        )

    async def _write_uncertain_recovery(
        self, task: ReplyTaskState, reason: str
    ) -> None:
        """升级期旧链路恢复：把"已 claim、结果不明"的旧任务收敛为 uncertain。

        新链路不产生 claimed 态；本方法保留一个版本周期后随旧事件类型一并
        删除（§3.4）。
        """
        await self._events.publish(
            event_type="runtime.reply_flushed",
            scope_key=task.scope_key,
            visibility="agent_visible",
            correlation_id=task.correlation_id or new_event_id(),
            causation_id=(
                task.source_tool_call_event_id or task.latest_event_id
            ),
            payload={
                "reply_task_id": task.reply_task_id,
                "revision": task.revision,
                "status": "uncertain",
                "message_ids": [],
                "sent_messages": [],
                "reason": reason,
            },
        )

"""ReplyExecutor 的生命周期完成合同（2026-07-31 删除 Replyer）。

到点只 complete + notify，不组稿、不碰 OneBot：
- 写入前在 scope 锁内复核 open / 最新 revision / 已到期；
- 同一最新 revision 只产生一条 runtime.reply_task_completed（causation 去重）；
- 事件先提交、后调注入的 notifier（supervisor 装配的 immediate-wake
  wrapper——publisher 协议不变，wrapper 内部才是 wake(immediate=True)）；
- 启动 rescan：重挂 open 定时器（过期立即触发）、升级期旧 flush 补
  uncertain；不建完成事件的消费审计/rescan outbox（best-effort 语义，
  §3.2）。
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

from qqbot.services.agent_loop.event_writer import RuntimeEventPublisher
from qqbot.services.agent_loop.reply_executor import ReplyExecutor
from qqbot.services.agent_loop.reply_task import ReplyTaskState

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 7, 31, 12, 0, tzinfo=TZ)


def _task(**overrides: object) -> ReplyTaskState:
    fields: dict = dict(
        reply_task_id="R1",
        scope_key="group:100",
        revision=2,
        state="open",
        created_at=NOW - timedelta(seconds=30),
        updated_at=NOW - timedelta(seconds=5),
        flush_at=NOW - timedelta(seconds=1),
        hard_deadline=NOW + timedelta(seconds=60),
        latest_event_id="E_UPSERT",
        source_tool_call_event_id="E_TOOL_CALL",
        correlation_id="CID",
    )
    fields.update(overrides)
    return ReplyTaskState(**fields)


class ReplyExecutorCompletionTests(unittest.TestCase):
    def _executor(
        self,
        notify: AsyncMock | None = None,
        write_event: AsyncMock | None = None,
    ) -> ReplyExecutor:
        publisher = RuntimeEventPublisher(
            lambda: None,  # type: ignore[arg-type]
            notify_event_available=notify,
            write_event=write_event or AsyncMock(return_value="E_COMPLETED"),
        )
        return ReplyExecutor(
            session_factory=lambda: None,
            event_publisher=publisher,
        )

    def _fire(
        self,
        executor: ReplyExecutor,
        *,
        current: ReplyTaskState | None,
        existing_completed: str | None = None,
        revision: int = 2,
    ) -> AsyncMock:
        find_completed = AsyncMock(return_value=existing_completed)

        async def run() -> None:
            with (
                patch(
                    "qqbot.services.agent_loop.reply_executor.scope_lock",
                    return_value=asyncio.Lock(),
                ),
                patch(
                    "qqbot.services.agent_loop.reply_executor.china_now",
                    return_value=NOW,
                ),
                patch(
                    "qqbot.services.agent_loop.reply_executor."
                    "load_open_reply_task",
                    new=AsyncMock(return_value=current),
                ),
                patch(
                    "qqbot.services.agent_loop.reply_executor."
                    "find_completed_for_upsert",
                    new=find_completed,
                ),
            ):
                await executor._fire("R1", revision)

        executor._find_open = AsyncMock(  # type: ignore[method-assign]
            return_value=current if current is not None else _task()
        )
        asyncio.run(run())
        return find_completed

    def test_due_task_completes_with_schedule_only_payload(self) -> None:
        notify = AsyncMock()
        write = AsyncMock(return_value="E_COMPLETED")
        executor = self._executor(notify, write)

        self._fire(executor, current=_task())

        kwargs = write.await_args.kwargs
        self.assertEqual(kwargs["event_type"], "runtime.reply_task_completed")
        self.assertEqual(kwargs["visibility"], "agent_visible")
        self.assertEqual(kwargs["causation_id"], "E_UPSERT")
        self.assertEqual(kwargs["correlation_id"], "CID")
        payload = kwargs["payload"]
        self.assertEqual(payload["reply_task_id"], "R1")
        self.assertEqual(payload["revision"], 2)
        self.assertEqual(
            payload["source_tool_call_event_id"], "E_TOOL_CALL"
        )
        self.assertIn("flush_at", payload)
        self.assertIn("hard_deadline", payload)
        self.assertIn("completed_at", payload)
        # 完成事件不是授权：没有任何 token/消费/TTL 语义的字段。
        for forbidden in ("authorization_id", "consumed", "expires_at", "ttl"):
            self.assertNotIn(forbidden, payload)
        # 2026-08-01 起也**不携带任何内容**：这条事件只是一次到点叫醒，说什么
        # 由醒来那一拍读当时的时间线决定。
        for gone in ("analysis", "brief", "targets", "gist"):
            self.assertNotIn(gone, payload)

    def test_notify_happens_after_persist(self) -> None:
        """persist-then-notify：wake 到达时投影必然读得到完成事件。immediate
        与否由 supervisor 注入的 wrapper 决定，不属于执行器的契约面。"""
        timeline: list[str] = []

        async def _write(*_: object, **__: object) -> str:
            timeline.append("persist")
            return "E_COMPLETED"

        async def _notify(scope_key: str) -> None:
            timeline.append(f"notify:{scope_key}")

        executor = self._executor(
            AsyncMock(side_effect=_notify), AsyncMock(side_effect=_write)
        )
        self._fire(executor, current=_task())
        self.assertEqual(timeline, ["persist", "notify:group:100"])

    def test_notify_failure_does_not_rewrite_the_completed_event(self) -> None:
        notify = AsyncMock(side_effect=RuntimeError("wake failed"))
        write = AsyncMock(return_value="E_COMPLETED")
        executor = self._executor(notify, write)
        self._fire(executor, current=_task())
        write.assert_awaited_once()
        notify.assert_awaited_once()

    def test_existing_completed_event_is_not_duplicated(self) -> None:
        """并发回调 / rescan 重入：同一最新 upsert 只允许一条完成事件。"""
        write = AsyncMock(return_value="E_COMPLETED")
        executor = self._executor(AsyncMock(), write)
        find = self._fire(
            executor, current=_task(), existing_completed="E_OLD_COMPLETED"
        )
        find.assert_awaited_once()
        write.assert_not_awaited()

    def test_stale_timer_reschedules_latest_revision_without_completing(
        self,
    ) -> None:
        """旧 revision 的回调不得替 append 后的最新授权拍板。"""
        current = _task(
            revision=3,
            flush_at=NOW + timedelta(seconds=20),
            latest_event_id="E_UPSERT_3",
        )
        write = AsyncMock(return_value="E_COMPLETED")
        executor = self._executor(AsyncMock(), write)
        executor._schedule = Mock()  # type: ignore[method-assign]

        self._fire(executor, current=current, revision=2)

        executor._schedule.assert_called_once_with(  # type: ignore[attr-defined]
            "R1", 3, current.flush_at
        )
        write.assert_not_awaited()

    def test_not_yet_due_reschedules_instead_of_completing(self) -> None:
        current = _task(flush_at=NOW + timedelta(seconds=15))
        write = AsyncMock(return_value="E_COMPLETED")
        executor = self._executor(AsyncMock(), write)
        executor._schedule = Mock()  # type: ignore[method-assign]

        self._fire(executor, current=current)

        executor._schedule.assert_called_once_with(  # type: ignore[attr-defined]
            "R1", 2, current.flush_at
        )
        write.assert_not_awaited()

    def test_cancelled_or_missing_draft_completes_nothing(self) -> None:
        write = AsyncMock(return_value="E_COMPLETED")
        executor = self._executor(AsyncMock(), write)
        self._fire(executor, current=None)
        write.assert_not_awaited()


class ReplyExecutorRescanTests(unittest.TestCase):
    def _executor(
        self,
        notify: AsyncMock | None = None,
        write_event: AsyncMock | None = None,
    ) -> ReplyExecutor:
        publisher = RuntimeEventPublisher(
            lambda: None,  # type: ignore[arg-type]
            notify_event_available=notify,
            write_event=write_event or AsyncMock(return_value="E_RUNTIME"),
        )
        return ReplyExecutor(
            session_factory=lambda: None,
            event_publisher=publisher,
        )

    def _start(
        self,
        executor: ReplyExecutor,
        *,
        recent: list[ReplyTaskState],
        has_claim: bool = False,
    ) -> None:
        with (
            patch(
                "qqbot.services.agent_loop.reply_executor."
                "load_recent_reply_tasks",
                new=AsyncMock(return_value=recent),
            ),
            patch(
                "qqbot.services.agent_loop.reply_executor.has_delivery_claim",
                new=AsyncMock(return_value=has_claim),
            ),
        ):
            asyncio.run(executor.start())

    def test_open_tasks_are_rescheduled_even_when_overdue(self) -> None:
        """已过 flush_at 的 open 任务重启后立即触发完成路径——完成没有聊天
        副作用，不再需要旧链路的 reply_task_overdue 提示事件。"""
        overdue = _task(flush_at=NOW - timedelta(seconds=120))
        executor = self._executor(AsyncMock())
        executor._schedule = Mock()  # type: ignore[method-assign]
        self._start(executor, recent=[overdue])
        executor._schedule.assert_called_once_with(  # type: ignore[attr-defined]
            "R1", 2, overdue.flush_at
        )

    def test_legacy_claimed_task_recovers_as_uncertain_flushed(self) -> None:
        """升级期旧 flush 恢复（§3.4）：旧链路 claimed 态补 uncertain，不再
        组稿重试。"""
        legacy = _task(state="claimed")
        notify = AsyncMock()
        write = AsyncMock(return_value="E_FINAL")
        executor = self._executor(notify, write)
        executor._schedule = Mock()  # type: ignore[method-assign]
        self._start(executor, recent=[legacy])
        kwargs = write.await_args.kwargs
        self.assertEqual(kwargs["event_type"], "runtime.reply_flushed")
        self.assertEqual(kwargs["payload"]["status"], "uncertain")
        self.assertEqual(kwargs["causation_id"], "E_TOOL_CALL")
        executor._schedule.assert_not_called()  # type: ignore[attr-defined]

    def test_open_task_with_durable_claim_recovers_as_uncertain(self) -> None:
        task = _task()
        write = AsyncMock(return_value="E_FINAL")
        executor = self._executor(AsyncMock(), write)
        executor._schedule = Mock()  # type: ignore[method-assign]
        self._start(executor, recent=[task], has_claim=True)
        kwargs = write.await_args.kwargs
        self.assertEqual(kwargs["payload"]["status"], "uncertain")
        self.assertIn("durable claim", kwargs["payload"]["reason"])
        # 已按 uncertain 收敛的任务不再重挂定时器。
        executor._schedule.assert_not_called()  # type: ignore[attr-defined]

    def test_runtime_only_event_does_not_notify_scope(self) -> None:
        notify = AsyncMock()
        write = AsyncMock(return_value="E_X")
        publisher = RuntimeEventPublisher(
            lambda: None,  # type: ignore[arg-type]
            notify_event_available=notify,
            write_event=write,
        )
        asyncio.run(
            publisher.publish(
                event_type="runtime.reply_flush_claimed",
                scope_key="group:100",
                visibility="runtime_only",
                correlation_id="CID",
                causation_id="E_UPSERT",
                payload={"reply_task_id": "R1"},
            )
        )
        write.assert_awaited_once()
        notify.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()

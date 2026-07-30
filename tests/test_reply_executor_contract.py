"""ReplyExecutor 的单次组稿、verbatim 旁路和最终事件合同。"""

from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

from qqbot.services.agent_loop.decision import DecisionContext
from qqbot.services.agent_loop.event_writer import RuntimeEventPublisher
from qqbot.services.agent_loop.reply_executor import (
    ReplyExecutor,
    _delivery_status,
    _public_receipt,
)
from qqbot.services.agent_loop.reply_task import ReplyTaskState

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 7, 19, 12, 0, tzinfo=TZ)
CHAT = [{"type": "text", "data": {"text": "精确文本"}}]


def _task(mode: str = "compose") -> ReplyTaskState:
    return ReplyTaskState(
        reply_task_id="R1",
        scope_key="group:100",
        revision=2,
        state="claimed",
        created_at=NOW,
        updated_at=NOW,
        flush_at=NOW,
        hard_deadline=NOW + timedelta(seconds=90),
        mode=mode,
        analysis="" if mode == "verbatim" else "当前完整对话分析",
        verbatim_messages=[{"content": CHAT}] if mode == "verbatim" else [],
        latest_event_id="E_UPSERT",
        source_tool_call_event_id="E_TOOL_CALL",
        correlation_id="CID",
    )


class _Projector:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def build_context(self, **kwargs: object) -> DecisionContext:
        self.calls.append(dict(kwargs))
        return DecisionContext(
            scope_key="group:100",
            correlation_id="CID",
            tick_seq=0,
            now=NOW,
        )


class _Replyer:
    def __init__(self) -> None:
        self.calls = 0
        self.analyses: list[str] = []

    async def compose(self, task: ReplyTaskState, *_: object) -> dict:
        self.calls += 1
        self.analyses.append(task.analysis)
        return {
            "messages": [{"kind": "chat", "content": CHAT}],
            "empty_reason": None,
        }


class ReplyExecutorContractTests(unittest.TestCase):
    def _executor(
        self,
        replyer: _Replyer,
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
            projector=_Projector(),
            event_publisher=publisher,
            replyer=replyer,  # type: ignore[arg-type]
        )

    def test_compose_context_carries_resolved_bot_user_id(self) -> None:
        # 2026-07-22：组稿 context 与 AgentLoop 同源带 bot_qq（supervisor 注入
        # 同一把 resolver），Replyer 判"谁在对谁说话"的输入不再低 Planner
        # 一等；resolver 抛异常降级 None，不挡 flush。
        projector = _Projector()
        executor = ReplyExecutor(
            session_factory=lambda: None,
            projector=projector,
            event_publisher=RuntimeEventPublisher(
                lambda: None,  # type: ignore[arg-type]
                write_event=AsyncMock(return_value="E_RUNTIME"),
            ),
            replyer=_Replyer(),  # type: ignore[arg-type]
            bot_user_id_resolver=lambda: "10001",
        )
        executor._preflight = AsyncMock(  # type: ignore[method-assign]
            return_value=([{"kind": "chat", "content": CHAT}], None)
        )
        executor._send_all = AsyncMock(return_value=[])  # type: ignore[method-assign]
        executor._write_flushed = AsyncMock()  # type: ignore[method-assign]
        asyncio.run(executor._compose_and_send(_task(), "E_CLAIM", "CID"))
        self.assertEqual(projector.calls[0]["bot_user_id"], "10001")

        broken = ReplyExecutor(
            session_factory=lambda: None,
            projector=projector,
            event_publisher=RuntimeEventPublisher(
                lambda: None,  # type: ignore[arg-type]
                write_event=AsyncMock(return_value="E_RUNTIME"),
            ),
            replyer=_Replyer(),  # type: ignore[arg-type]
            bot_user_id_resolver=lambda: (_ for _ in ()).throw(
                RuntimeError("registry down")
            ),
        )
        broken._preflight = AsyncMock(  # type: ignore[method-assign]
            return_value=([{"kind": "chat", "content": CHAT}], None)
        )
        broken._send_all = AsyncMock(return_value=[])  # type: ignore[method-assign]
        broken._write_flushed = AsyncMock()  # type: ignore[method-assign]
        asyncio.run(broken._compose_and_send(_task(), "E_CLAIM", "CID"))
        self.assertIsNone(projector.calls[1]["bot_user_id"])

    def test_compose_task_calls_replyer_once_and_only_writes_final(self) -> None:
        replyer = _Replyer()
        notify = AsyncMock()
        executor = self._executor(replyer, notify)
        receipt = {
            "index": 0,
            "kind": "chat",
            "content": CHAT,
            "status": "sent",
            "message_id": 123,
            "self_id": "10001",
        }
        executor._preflight = AsyncMock(  # type: ignore[method-assign]
            return_value=([{"kind": "chat", "content": CHAT}], None)
        )
        executor._send_all = AsyncMock(  # type: ignore[method-assign]
            return_value=[receipt]
        )
        executor._write_flushed = AsyncMock()  # type: ignore[method-assign]

        asyncio.run(executor._compose_and_send(_task(), "E_CLAIM", "CID"))

        self.assertEqual(replyer.calls, 1)
        self.assertEqual(replyer.analyses, ["当前完整对话分析"])
        executor._write_flushed.assert_awaited_once()  # type: ignore[attr-defined]
        kwargs = executor._write_flushed.await_args.kwargs  # type: ignore[attr-defined]
        self.assertEqual(kwargs["status"], "sent")
        self.assertEqual(kwargs["sent_messages"], [receipt])
        # ReplyExecutor 只负责写 final；事件发布器负责落库后的通用通知。
        notify.assert_not_awaited()

    def test_verbatim_bypasses_replyer(self) -> None:
        replyer = _Replyer()
        executor = self._executor(replyer, AsyncMock())
        executor._preflight = AsyncMock(  # type: ignore[method-assign]
            return_value=([{"kind": "chat", "content": CHAT}], None)
        )
        executor._send_all = AsyncMock(  # type: ignore[method-assign]
            return_value=[
                {
                    "index": 0,
                    "kind": "chat",
                    "content": CHAT,
                    "status": "sent",
                    "message_id": 123,
                }
            ]
        )
        executor._write_flushed = AsyncMock()  # type: ignore[method-assign]

        asyncio.run(
            executor._compose_and_send(
                _task("verbatim"), "E_CLAIM", "CID"
            )
        )
        self.assertEqual(replyer.calls, 0)

    def test_event_notification_failure_does_not_rewrite_final(self) -> None:
        notify = AsyncMock(side_effect=RuntimeError("notify failed"))
        write_event = AsyncMock(return_value="E_FINAL")
        executor = self._executor(_Replyer(), notify, write_event)

        asyncio.run(
            executor._write_flushed(
                _task(),
                "E_CLAIM",
                "CID",
                status="failed",
                sent_messages=[],
                cutoff_event_id="E_CUTOFF",
                reason="upstream failed",
            )
        )

        write_event.assert_awaited_once()
        notify.assert_awaited_once_with("group:100")

    def test_final_persistence_failure_is_never_retried_inline(self) -> None:
        replyer = _Replyer()
        notify = AsyncMock()
        executor = self._executor(replyer, notify)
        executor._preflight = AsyncMock(  # type: ignore[method-assign]
            return_value=([{"kind": "chat", "content": CHAT}], None)
        )
        executor._send_all = AsyncMock(  # type: ignore[method-assign]
            return_value=[
                {
                    "index": 0,
                    "kind": "chat",
                    "content": CHAT,
                    "status": "sent",
                    "message_id": 123,
                }
            ]
        )
        executor._write_flushed = AsyncMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("commit outcome unknown")
        )

        asyncio.run(executor._compose_and_send(_task(), "E_CLAIM", "CID"))

        executor._write_flushed.assert_awaited_once()  # type: ignore[attr-defined]
        notify.assert_not_awaited()

    def test_final_event_anchors_latest_reply_tool_call(self) -> None:
        timeline: list[str] = []

        async def _write(*_: object, **__: object) -> str:
            timeline.append("persist")
            return "E_FINAL"

        async def _notify(_: str) -> None:
            timeline.append("notify")

        notify = AsyncMock(side_effect=_notify)
        write = AsyncMock(side_effect=_write)
        executor = self._executor(_Replyer(), notify, write)
        asyncio.run(
            executor._write_flushed(
                _task(),
                "E_CLAIM",
                "CID",
                status="sent",
                sent_messages=[
                    {
                        "kind": "chat",
                        "content": CHAT,
                        "status": "sent",
                        "message_id": 123,
                    }
                ],
                cutoff_event_id="E_CUTOFF",
            )
        )
        kwargs = write.await_args.kwargs
        self.assertEqual(kwargs["event_type"], "runtime.reply_flushed")
        self.assertEqual(kwargs["visibility"], "agent_visible")
        self.assertEqual(kwargs["causation_id"], "E_TOOL_CALL")
        self.assertEqual(kwargs["payload"]["message_ids"], [123])
        notify.assert_awaited_once_with("group:100")
        self.assertEqual(timeline, ["persist", "notify"])

    def test_runtime_only_event_does_not_notify_scope(self) -> None:
        notify = AsyncMock()
        write = AsyncMock(return_value="E_CLAIM")
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

    def test_public_receipt_redacts_base64_and_binary_values(self) -> None:
        receipt = _public_receipt(
            {
                "status": "ok",
                "echo": {
                    "message": [
                        {
                            "file": "base64://secret-payload",
                            "raw": b"secret-bytes",
                        }
                    ]
                },
            }
        )

        self.assertEqual(receipt["status"], "ok")
        self.assertEqual(
            receipt["echo"]["message"][0]["file"], "<base64-redacted>"
        )
        self.assertEqual(
            receipt["echo"]["message"][0]["raw"], "<binary-redacted>"
        )

    def test_delivery_status_keeps_unknown_delivery_distinct(self) -> None:
        self.assertEqual(_delivery_status([{"status": "sent"}]), "sent")
        self.assertEqual(
            _delivery_status([{"status": "sent"}, {"status": "failed"}]),
            "partial",
        )
        self.assertEqual(
            _delivery_status([{"status": "uncertain"}]), "uncertain"
        )
        self.assertEqual(_delivery_status([{"status": "failed"}]), "failed")

    def test_stale_timer_reschedules_latest_revision_without_claiming(
        self,
    ) -> None:
        """旧 revision 的回调不得抢占 append 后的最新授权。"""
        stale = replace(_task(), state="open", revision=1)
        current = replace(
            stale,
            revision=2,
            flush_at=NOW + timedelta(seconds=20),
            latest_event_id="E_UPSERT_2",
        )
        replyer = _Replyer()
        write_event = AsyncMock(return_value="E_RUNTIME")
        executor = self._executor(replyer, AsyncMock(), write_event)
        executor._find_open = AsyncMock(  # type: ignore[method-assign]
            return_value=stale
        )
        executor._schedule = Mock()  # type: ignore[method-assign]

        async def run() -> None:
            with (
                patch(
                    "qqbot.services.agent_loop.reply_executor.scope_lock",
                    return_value=asyncio.Lock(),
                ),
                patch(
                    "qqbot.services.agent_loop.reply_executor.load_open_reply_task",
                    new=AsyncMock(return_value=current),
                ),
                patch(
                    "qqbot.services.agent_loop.reply_executor.try_claim_once_strict",
                    new=AsyncMock(),
                ) as claim,
            ):
                await executor._fire("R1", 1)

            executor._schedule.assert_called_once_with(  # type: ignore[attr-defined]
                "R1", 2, current.flush_at
            )
            claim.assert_not_awaited()
            write_event.assert_not_awaited()

        asyncio.run(run())
        self.assertEqual(replyer.calls, 0)

    def test_start_recovers_durable_claim_without_claim_event(self) -> None:
        task = replace(_task(), state="open")
        notify = AsyncMock()
        write = AsyncMock(return_value="E_FINAL")
        executor = self._executor(_Replyer(), notify, write)
        with (
            patch(
                "qqbot.services.agent_loop.reply_executor.load_recent_reply_tasks",
                new=AsyncMock(return_value=[task]),
            ),
            patch(
                "qqbot.services.agent_loop.reply_executor.has_delivery_claim",
                new=AsyncMock(return_value=True),
            ) as has_claim,
        ):
            asyncio.run(executor.start())

        has_claim.assert_awaited_once_with(  # type: ignore[attr-defined]
            executor._session_factory, "E_UPSERT", "reply_flush"
        )
        kwargs = write.await_args.kwargs
        self.assertEqual(kwargs["event_type"], "runtime.reply_flushed")
        self.assertEqual(kwargs["payload"]["status"], "uncertain")
        self.assertIn("durable claim", kwargs["payload"]["reason"])
        notify.assert_awaited_once_with("group:100")


if __name__ == "__main__":
    unittest.main()

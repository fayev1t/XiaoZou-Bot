"""ReplyExecutor 的单次组稿、verbatim 旁路和最终事件合同。"""

from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from qqbot.services.agent_loop.decision import DecisionContext
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
        targets=[{"message_id": "M1", "points": ["回答"]}],
        gist={"intent": "解释"},
        verbatim_messages=[{"content": CHAT}] if mode == "verbatim" else [],
        latest_event_id="E_UPSERT",
        source_tool_call_event_id="E_TOOL_CALL",
        correlation_id="CID",
    )


class _Projector:
    async def build_context(self, **_: object) -> DecisionContext:
        return DecisionContext(
            scope_key="group:100",
            correlation_id="CID",
            tick_seq=0,
            now=NOW,
        )


class _Replyer:
    def __init__(self) -> None:
        self.calls = 0

    async def compose(self, *_: object) -> dict:
        self.calls += 1
        return {
            "messages": [{"kind": "chat", "content": CHAT}],
            "empty_reason": None,
        }


class ReplyExecutorContractTests(unittest.TestCase):
    def _executor(self, replyer: _Replyer, wake: AsyncMock) -> ReplyExecutor:
        return ReplyExecutor(
            session_factory=lambda: None,
            projector=_Projector(),
            wake_scope=wake,
            replyer=replyer,  # type: ignore[arg-type]
        )

    def test_compose_task_calls_replyer_once_and_success_wakes_once(self) -> None:
        replyer = _Replyer()
        wake = AsyncMock()
        executor = self._executor(replyer, wake)
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
        executor._write_flushed.assert_awaited_once()  # type: ignore[attr-defined]
        kwargs = executor._write_flushed.await_args.kwargs  # type: ignore[attr-defined]
        self.assertEqual(kwargs["status"], "sent")
        self.assertEqual(kwargs["sent_messages"], [receipt])
        # 2026-07-22 起 flush 成功同样唤醒一次（final 先落库、唤醒在后）。
        wake.assert_awaited_once_with("group:100")

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

    def test_failure_wake_error_does_not_write_a_second_final(self) -> None:
        replyer = _Replyer()
        wake = AsyncMock(side_effect=RuntimeError("wake failed"))
        executor = self._executor(replyer, wake)
        executor._preflight = AsyncMock(  # type: ignore[method-assign]
            return_value=([{"kind": "chat", "content": CHAT}], None)
        )
        executor._send_all = AsyncMock(  # type: ignore[method-assign]
            return_value=[
                {
                    "index": 0,
                    "kind": "chat",
                    "content": CHAT,
                    "status": "failed",
                    "error": {"kind": "upstream_action_failed"},
                }
            ]
        )
        executor._write_flushed = AsyncMock()  # type: ignore[method-assign]

        asyncio.run(executor._compose_and_send(_task(), "E_CLAIM", "CID"))

        executor._write_flushed.assert_awaited_once()  # type: ignore[attr-defined]
        await_args = getattr(executor._write_flushed, "await_args")
        self.assertEqual(await_args.kwargs["status"], "failed")

    def test_final_persistence_failure_is_never_retried_inline(self) -> None:
        replyer = _Replyer()
        wake = AsyncMock()
        executor = self._executor(replyer, wake)
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
        wake.assert_not_awaited()

    def test_final_event_anchors_latest_reply_tool_call(self) -> None:
        executor = self._executor(_Replyer(), AsyncMock())
        with patch(
            "qqbot.services.agent_loop.reply_executor.write_runtime_event",
            new=AsyncMock(return_value="E_FINAL"),
        ) as write:
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

    def test_start_recovers_durable_claim_without_claim_event(self) -> None:
        task = replace(_task(), state="open")
        executor = self._executor(_Replyer(), AsyncMock())
        with (
            patch(
                "qqbot.services.agent_loop.reply_executor.load_recent_reply_tasks",
                new=AsyncMock(return_value=[task]),
            ),
            patch(
                "qqbot.services.agent_loop.reply_executor.has_delivery_claim",
                new=AsyncMock(return_value=True),
            ) as has_claim,
            patch(
                "qqbot.services.agent_loop.reply_executor.write_runtime_event",
                new=AsyncMock(return_value="E_FINAL"),
            ) as write,
        ):
            asyncio.run(executor.start())

        has_claim.assert_awaited_once_with(  # type: ignore[attr-defined]
            executor._session_factory, "E_UPSERT", "reply_flush"
        )
        kwargs = write.await_args.kwargs
        self.assertEqual(kwargs["event_type"], "runtime.reply_flushed")
        self.assertEqual(kwargs["payload"]["status"], "uncertain")
        self.assertIn("durable claim", kwargs["payload"]["reason"])


if __name__ == "__main__":
    unittest.main()

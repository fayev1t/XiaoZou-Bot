"""Contract tests for ReplySendWorker.

Covers (任务与决策契约 §6, dispatcher 设计 2026-05-26):
- happy path: registered bot + group target → send_group_msg called →
  agent.reply_delivered written with onebot_message_id
- send raises → agent.reply_failed written (no delivered)
- no bot in registry → agent.reply_failed with no_bot_available
- target.kind=private → send_private_msg called
- unknown target.kind → agent.reply_failed

We exercise `_process_one()` directly (skipping the DB SELECT) so the test
stays a pure unit test: a recording session captures every insert.
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from typing import Any

from qqbot.services.agent_loop import bot_registry
from qqbot.services.agent_loop.reply_worker import ReplySendWorker


class _RecordingSession:
    def __init__(self, store: list[Any]) -> None:
        self._store = store

    async def execute(self, stmt: Any) -> Any:
        self._store.append(stmt)
        return SimpleNamespace(rowcount=1)

    async def commit(self) -> None:
        return None

    async def __aenter__(self) -> "_RecordingSession":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None


def _factory_for(store: list[Any]):
    def factory() -> _RecordingSession:
        return _RecordingSession(store)

    return factory


def _types(captured: list[Any]) -> list[str]:
    return [stmt.compile().params.get("type") for stmt in captured]


def _payloads_by_type(captured: list[Any]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for stmt in captured:
        params = stmt.compile().params
        out[params.get("type")] = params.get("payload") or {}
    return out


class _StubBot:
    def __init__(
        self,
        self_id: str = "11111",
        raise_on_send: Exception | None = None,
        return_value: Any = None,
    ) -> None:
        self.self_id = self_id
        self._raise = raise_on_send
        self._ret = return_value if return_value is not None else {"message_id": 999}
        self.calls: list[tuple[str, int, Any]] = []

    async def send_group_msg(self, group_id: int, message: Any) -> Any:
        self.calls.append(("group", group_id, message))
        if self._raise:
            raise self._raise
        return self._ret

    async def send_private_msg(self, user_id: int, message: Any) -> Any:
        self.calls.append(("private", user_id, message))
        if self._raise:
            raise self._raise
        return self._ret


def _row(
    *,
    event_id: str = "EID",
    scope: str = "group",
    group_id: int | None = 100,
    user_id: int | None = None,
    correlation_id: str = "CID",
    payload: dict | None = None,
) -> dict:
    return {
        "event_id": event_id,
        "scope": scope,
        "group_id": group_id,
        "user_id": user_id,
        "correlation_id": correlation_id,
        "payload": payload or {},
    }


class ReplyWorkerContractTest(unittest.TestCase):
    def setUp(self) -> None:
        bot_registry.clear()

    def tearDown(self) -> None:
        bot_registry.clear()

    def test_group_happy_path_writes_reply_delivered(self) -> None:
        bot = _StubBot(self_id="42", return_value={"message_id": 12345})
        bot_registry.register(bot)
        store: list[Any] = []
        worker = ReplySendWorker(session_factory=_factory_for(store))

        row = _row(
            event_id="EID1",
            scope="group",
            group_id=100,
            payload={
                "reply_id": "RID1",
                "content": [{"type": "text", "data": {"text": "hi"}}],
                "target": {"kind": "group", "group_id": 100},
            },
        )
        asyncio.run(worker._process_one(row))

        self.assertEqual(len(bot.calls), 1)
        kind, group_id, msg = bot.calls[0]
        self.assertEqual(kind, "group")
        self.assertEqual(group_id, 100)
        self.assertEqual(msg, [{"type": "text", "data": {"text": "hi"}}])

        types = _types(store)
        self.assertIn("agent.reply_delivered", types)
        self.assertNotIn("agent.reply_failed", types)

        payloads = _payloads_by_type(store)
        delivered = payloads["agent.reply_delivered"]
        self.assertEqual(delivered["reply_id"], "RID1")
        self.assertEqual(delivered["onebot_message_id"], 12345)
        self.assertEqual(delivered["self_id"], "42")

    def test_send_failure_writes_reply_failed(self) -> None:
        bot = _StubBot(raise_on_send=RuntimeError("napcat down"))
        bot_registry.register(bot)
        store: list[Any] = []
        worker = ReplySendWorker(session_factory=_factory_for(store))

        row = _row(
            event_id="EID2",
            payload={
                "reply_id": "RID2",
                "content": [{"type": "text", "data": {"text": "x"}}],
                "target": {"kind": "group", "group_id": 100},
            },
        )
        asyncio.run(worker._process_one(row))

        types = _types(store)
        self.assertIn("agent.reply_failed", types)
        self.assertNotIn("agent.reply_delivered", types)

        failed = _payloads_by_type(store)["agent.reply_failed"]
        self.assertEqual(failed["reply_id"], "RID2")
        self.assertEqual(failed["error_kind"], "RuntimeError")
        self.assertIn("napcat down", failed["error_message"])

    def test_no_bot_in_registry_writes_reply_failed(self) -> None:
        store: list[Any] = []
        worker = ReplySendWorker(session_factory=_factory_for(store))

        row = _row(
            event_id="EID3",
            payload={
                "reply_id": "RID3",
                "content": [],
                "target": {"kind": "group", "group_id": 100},
            },
        )
        asyncio.run(worker._process_one(row))

        failed = _payloads_by_type(store)["agent.reply_failed"]
        self.assertEqual(failed["error_kind"], "RuntimeError")
        self.assertEqual(failed["error_message"], "no_bot_available")

    def test_private_target_uses_send_private_msg(self) -> None:
        bot = _StubBot()
        bot_registry.register(bot)
        store: list[Any] = []
        worker = ReplySendWorker(session_factory=_factory_for(store))

        row = _row(
            event_id="EID4",
            scope="system",
            group_id=None,
            payload={
                "reply_id": "RID4",
                "content": [{"type": "text", "data": {"text": "yo"}}],
                "target": {"kind": "private", "user_id": 555},
            },
        )
        asyncio.run(worker._process_one(row))

        kind, uid, _ = bot.calls[0]
        self.assertEqual(kind, "private")
        self.assertEqual(uid, 555)
        self.assertIn("agent.reply_delivered", _types(store))

    def test_unknown_target_kind_writes_reply_failed(self) -> None:
        bot = _StubBot()
        bot_registry.register(bot)
        store: list[Any] = []
        worker = ReplySendWorker(session_factory=_factory_for(store))

        row = _row(
            event_id="EID5",
            payload={
                "reply_id": "RID5",
                "content": [],
                "target": {"kind": "nobody"},
            },
        )
        asyncio.run(worker._process_one(row))

        failed = _payloads_by_type(store)["agent.reply_failed"]
        self.assertEqual(failed["error_kind"], "ValueError")

    def test_self_id_in_target_routes_to_specific_bot(self) -> None:
        bot_a = _StubBot(self_id="A", return_value={"message_id": 1})
        bot_b = _StubBot(self_id="B", return_value={"message_id": 2})
        bot_registry.register(bot_a)
        bot_registry.register(bot_b)
        store: list[Any] = []
        worker = ReplySendWorker(session_factory=_factory_for(store))

        row = _row(
            event_id="EID6",
            payload={
                "reply_id": "RID6",
                "content": [],
                "target": {"kind": "group", "group_id": 100, "self_id": "B"},
            },
        )
        asyncio.run(worker._process_one(row))

        # bot B got the call, bot A did not
        self.assertEqual(len(bot_a.calls), 0)
        self.assertEqual(len(bot_b.calls), 1)
        delivered = _payloads_by_type(store)["agent.reply_delivered"]
        self.assertEqual(delivered["self_id"], "B")


if __name__ == "__main__":
    unittest.main()

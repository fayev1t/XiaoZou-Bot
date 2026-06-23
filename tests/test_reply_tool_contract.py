"""Contract tests for ReplyTool.

Covers:
- happy path: scope-matched group target writes agent.reply_emitted with
  the right payload and triggers the notify_reply_pending callback.
- target.kind mismatch → ValueError (ToolWorker converts to tool_failed).
- group_id mismatch → ValueError.
- private target on private scope → happy.
- private target with mismatched user_id → ValueError.
- empty content → ValueError.
- missing scope_key / correlation_id / session_factory in context → ValueError.
- wake callback failures swallowed (do not raise out of run()).
- usage_prompt loaded from sibling reply.md.

统一接口后 ReplyTool 无构造依赖：session_factory（写事件）与
notify_reply_pending（唤醒 ReplySendWorker）都从 run() 的 context 进，
由 ToolWorker 注入。这里用 stub session 直接喂进 context 验证调用面。

The session is a recording stub — we only verify write_agent_event was
called and the payload shape matches the old ReplyAction path.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any

from qqbot.services.agent_loop.tools.reply import ReplyTool


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


def _values(stmt: Any) -> dict:
    return {k: v for k, v in stmt.compile().params.items()}


class ReplyToolHappyPathTests(unittest.IsolatedAsyncioTestCase):
    async def test_group_target_writes_reply_emitted_event(self) -> None:
        captured: list[Any] = []
        wakes: list[int] = []
        tool = ReplyTool()

        result = await tool.run(
            {
                "content": [{"type": "text", "data": {"text": "hi"}}],
                "target": {"kind": "group", "group_id": 100},
                "related_msg_hashes": ["h1"],
            },
            scope_key="group:100",
            correlation_id="CID",
            session_factory=_factory_for(captured),
            notify_reply_pending=lambda: wakes.append(1),
        )

        # 写了恰好一条事件
        self.assertEqual(len(captured), 1)
        params = _values(captured[0])
        self.assertEqual(params["type"], "agent.reply_emitted")
        self.assertEqual(params["scope"], "group")
        self.assertEqual(params["group_id"], 100)
        self.assertEqual(params["correlation_id"], "CID")
        payload = params["payload"]
        self.assertEqual(
            payload["content"],
            [{"type": "text", "data": {"text": "hi"}}],
        )
        self.assertEqual(payload["target"], {"kind": "group", "group_id": 100})
        self.assertEqual(payload["related_msg_hashes"], ["h1"])
        self.assertIsNotNone(payload["reply_id"])

        # 回调被触发过一次
        self.assertEqual(len(wakes), 1)
        # tool_result 形状
        self.assertEqual(result["queued"], True)
        self.assertEqual(result["reply_event_id"], payload["reply_id"])

    async def test_private_target_writes_event(self) -> None:
        captured: list[Any] = []
        tool = ReplyTool()
        await tool.run(
            {
                "content": [{"type": "text", "data": {"text": "hi"}}],
                "target": {"kind": "private", "user_id": 555},
            },
            scope_key="private:555",
            correlation_id="CID",
            session_factory=_factory_for(captured),
        )
        self.assertEqual(len(captured), 1)
        self.assertEqual(_values(captured[0])["scope"], "private")


class ReplyToolValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_kind_mismatch_raises(self) -> None:
        tool = ReplyTool()
        with self.assertRaises(ValueError) as cm:
            await tool.run(
                {
                    "content": [{"type": "text", "data": {"text": "hi"}}],
                    "target": {"kind": "private", "user_id": 1},
                },
                scope_key="group:100",
                correlation_id="CID",
                session_factory=_factory_for([]),
            )
        self.assertIn("target.kind", str(cm.exception))

    async def test_group_id_mismatch_raises(self) -> None:
        tool = ReplyTool()
        with self.assertRaises(ValueError) as cm:
            await tool.run(
                {
                    "content": [{"type": "text", "data": {"text": "hi"}}],
                    "target": {"kind": "group", "group_id": 99999},
                },
                scope_key="group:100",
                correlation_id="CID",
                session_factory=_factory_for([]),
            )
        self.assertIn("group_id", str(cm.exception))

    async def test_private_user_id_mismatch_raises(self) -> None:
        tool = ReplyTool()
        with self.assertRaises(ValueError):
            await tool.run(
                {
                    "content": [{"type": "text", "data": {"text": "hi"}}],
                    "target": {"kind": "private", "user_id": 999},
                },
                scope_key="private:111",
                correlation_id="CID",
                session_factory=_factory_for([]),
            )

    async def test_missing_content_raises(self) -> None:
        tool = ReplyTool()
        with self.assertRaises(ValueError):
            await tool.run(
                {
                    "content": [],
                    "target": {"kind": "group", "group_id": 100},
                },
                scope_key="group:100",
                correlation_id="CID",
                session_factory=_factory_for([]),
            )

    async def test_missing_scope_key_raises(self) -> None:
        tool = ReplyTool()
        with self.assertRaises(ValueError):
            await tool.run(
                {
                    "content": [{"type": "text", "data": {"text": "hi"}}],
                    "target": {"kind": "group", "group_id": 100},
                },
                correlation_id="CID",
                session_factory=_factory_for([]),
            )

    async def test_missing_correlation_id_raises(self) -> None:
        tool = ReplyTool()
        with self.assertRaises(ValueError):
            await tool.run(
                {
                    "content": [{"type": "text", "data": {"text": "hi"}}],
                    "target": {"kind": "group", "group_id": 100},
                },
                scope_key="group:100",
                session_factory=_factory_for([]),
            )

    async def test_missing_session_factory_raises(self) -> None:
        # 统一接口后 session_factory 由 ToolWorker 注入；缺失说明调用方没按
        # 约定注入系统依赖 → 明确 raise（ToolWorker 据此写 tool_failed）。
        tool = ReplyTool()
        with self.assertRaises(ValueError) as cm:
            await tool.run(
                {
                    "content": [{"type": "text", "data": {"text": "hi"}}],
                    "target": {"kind": "group", "group_id": 100},
                },
                scope_key="group:100",
                correlation_id="CID",
            )
        self.assertIn("session_factory", str(cm.exception))


class ReplyToolWakeCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_notify_callback_is_noop(self) -> None:
        captured: list[Any] = []
        tool = ReplyTool()
        # context 不带 notify_reply_pending；应当不抛、事件仍写
        await tool.run(
            {
                "content": [{"type": "text", "data": {"text": "hi"}}],
                "target": {"kind": "group", "group_id": 100},
            },
            scope_key="group:100",
            correlation_id="CID",
            session_factory=_factory_for(captured),
        )
        self.assertEqual(len(captured), 1)

    async def test_notify_callback_exception_swallowed(self) -> None:
        def boom() -> None:
            raise RuntimeError("worker is on fire")

        captured: list[Any] = []
        tool = ReplyTool()
        # 回调抛错不应当传播：事件仍写，结果仍返回
        result = await tool.run(
            {
                "content": [{"type": "text", "data": {"text": "hi"}}],
                "target": {"kind": "group", "group_id": 100},
            },
            scope_key="group:100",
            correlation_id="CID",
            session_factory=_factory_for(captured),
            notify_reply_pending=boom,
        )
        self.assertEqual(len(captured), 1)
        self.assertTrue(result["queued"])


class ReplyToolMetadataTests(unittest.TestCase):
    def test_name_and_schema(self) -> None:
        # name 必须叫 "reply" —— LLM 通过 tool_name="reply" 调它
        self.assertEqual(ReplyTool.name, "reply")
        # 必填字段
        required = ReplyTool.arguments_schema["required"]
        self.assertIn("content", required)
        self.assertIn("target", required)

    def test_usage_prompt_loaded_from_sibling_md(self) -> None:
        # tools/reply.md 内容必须随工具被 ToolRegistry.usage_docs 拾取
        self.assertIn("your one and only way to speak", ReplyTool.usage_prompt)
        self.assertIn('"type": "at"', ReplyTool.usage_prompt)


if __name__ == "__main__":
    unittest.main()

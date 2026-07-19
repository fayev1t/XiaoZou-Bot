"""Contract tests for SendMessageTool（同步发送）。

send_message 已是**同步**工具：run() 直接经 bot_registry 调 napcat
（send_group_msg / send_private_msg），**永不 raise**——无论成功失败都返回一个
`ToolOutcome`（成功带 message_id；失败带语义化 error_kind）。这里用 stub Bot
注册进 bot_registry 验证调用面 + 结构化输出。测试**无一处 assertRaises**。
"""

from __future__ import annotations

import unittest
from typing import Any

from qqbot.services.agent_loop import bot_registry
from qqbot.services.agent_loop.tools.send_message import SendMessageTool


class _FakeActionFailed(Exception):
    """模拟 nonebot OneBot v11 ActionFailed：完整响应挂在 .info（含 retcode /
    message / wording / status / stream）。call_action 据此折成
    upstream_action_failed 失败 outcome（透传 upstream_* / stream）。"""

    def __init__(
        self,
        retcode: int,
        wording: str,
        *,
        message: str = "",
        stream: str = "normal-action",
    ) -> None:
        super().__init__(f"ActionFailed: retcode={retcode}")
        self.info = {
            "status": "failed",
            "retcode": retcode,
            "message": message,
            "wording": wording,
            "stream": stream,
        }


class _StubBot:
    def __init__(
        self,
        self_id: str = "10001",
        message_id: int | None = 12345,
        raise_exc: Exception | None = None,
    ) -> None:
        self.self_id = self_id
        self._message_id = message_id
        self._raise = raise_exc
        self.calls: list[tuple[str, dict]] = []

    def _result(self) -> dict:
        # message_id=None 模拟 napcat status=ok 但没回 message_id 的畸形成功包。
        return {"message_id": self._message_id} if self._message_id is not None else {}

    async def send_group_msg(self, **kwargs: Any) -> dict:
        self.calls.append(("send_group_msg", kwargs))
        if self._raise is not None:
            raise self._raise
        return self._result()

    async def send_private_msg(self, **kwargs: Any) -> dict:
        self.calls.append(("send_private_msg", kwargs))
        if self._raise is not None:
            raise self._raise
        return self._result()


class SendMessageToolHappyPathTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        bot_registry.clear()

    def tearDown(self) -> None:
        bot_registry.clear()

    async def test_group_send_returns_message_id(self) -> None:
        bot = _StubBot(message_id=999)
        bot_registry.register(bot)
        outcome = await SendMessageTool().run(
            {
                "content": [{"type": "text", "data": {"text": "hi"}}],
                "target": {"kind": "group", "group_id": 100},
            },
            scope_key="group:100",
        )
        self.assertEqual(len(bot.calls), 1)
        method, kwargs = bot.calls[0]
        self.assertEqual(method, "send_group_msg")
        self.assertEqual(kwargs["group_id"], 100)
        self.assertEqual(
            kwargs["message"], [{"type": "text", "data": {"text": "hi"}}]
        )
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.result["message_id"], 999)
        self.assertEqual(outcome.result["self_id"], "10001")
        self.assertNotIn("related_image_hashes", outcome.result)
        self.assertTrue(outcome.result["sent"])

    async def test_private_send(self) -> None:
        bot = _StubBot()
        bot_registry.register(bot)
        await SendMessageTool().run(
            {
                "content": [{"type": "text", "data": {"text": "hi"}}],
                "target": {"kind": "private", "user_id": 555},
            },
            scope_key="private:555",
        )
        method, kwargs = bot.calls[0]
        self.assertEqual(method, "send_private_msg")
        self.assertEqual(kwargs["user_id"], 555)

    async def test_plain_text_only_is_legal_no_reply_segment_required(
        self,
    ) -> None:
        """产品边界（send_message ≠ QQ reply 段）：纯 text 发言完全合法，
        不强制携带 {"type":"reply"} 引用段——普通聊天的默认形态就是纯文本。"""
        bot = _StubBot(message_id=7)
        bot_registry.register(bot)
        outcome = await SendMessageTool().run(
            {
                "content": [
                    {"type": "text", "data": {"text": "带伞啦,明天有雨"}}
                ],
                "target": {"kind": "group", "group_id": 100},
            },
            scope_key="group:100",
        )
        self.assertTrue(outcome.ok)
        _method, kwargs = bot.calls[0]
        # 原样透传：不得被自动塞进 reply/at 段
        self.assertEqual(
            [seg["type"] for seg in kwargs["message"]], ["text"]
        )

    async def test_reply_at_text_combo_succeeds(self) -> None:
        bot = _StubBot(message_id=42)
        bot_registry.register(bot)
        outcome = await SendMessageTool().run(
            {
                "content": [
                    {"type": "reply", "data": {"id": "M1"}},
                    {"type": "at", "data": {"qq": "99999"}},
                    {"type": "text", "data": {"text": " hi"}},
                ],
                "target": {"kind": "group", "group_id": 100},
            },
            scope_key="group:100",
        )
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.result["message_id"], 42)

    async def test_text_face_combo_succeeds(self) -> None:
        bot_registry.register(_StubBot())
        outcome = await SendMessageTool().run(
            {
                "content": [
                    {"type": "text", "data": {"text": "nice"}},
                    {"type": "face", "data": {"id": "178"}},
                ],
                "target": {"kind": "private", "user_id": 555},
            },
            scope_key="private:555",
        )
        self.assertTrue(outcome.ok)

    async def test_at_all_accepted(self) -> None:
        bot_registry.register(_StubBot())
        outcome = await SendMessageTool().run(
            {
                "content": [
                    {"type": "at", "data": {"qq": "all"}},
                    {"type": "text", "data": {"text": "开会啦"}},
                ],
                "target": {"kind": "group", "group_id": 100},
            },
            scope_key="group:100",
        )
        self.assertTrue(outcome.ok)


class SendMessageToolTargetScopeTests(unittest.IsolatedAsyncioTestCase):
    """target 结构对但发错会话 → target_scope_mismatch（独立于结构类
    invalid_arguments，见设计 §7）。"""

    def setUp(self) -> None:
        bot_registry.clear()

    def tearDown(self) -> None:
        bot_registry.clear()

    async def test_kind_mismatch_is_target_scope_mismatch(self) -> None:
        bot = _StubBot()
        bot_registry.register(bot)
        outcome = await SendMessageTool().run(
            {
                "content": [{"type": "text", "data": {"text": "hi"}}],
                "target": {"kind": "private", "user_id": 1},
            },
            scope_key="group:100",
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "target_scope_mismatch")
        self.assertEqual(outcome.extra["expected_scope"], "group:100")
        self.assertEqual(outcome.extra["actual_target_kind"], "private")
        self.assertEqual(bot.calls, [])  # 没真正发送

    async def test_group_id_mismatch_is_target_scope_mismatch(self) -> None:
        bot = _StubBot()
        bot_registry.register(bot)
        outcome = await SendMessageTool().run(
            {
                "content": [{"type": "text", "data": {"text": "hi"}}],
                "target": {"kind": "group", "group_id": 99999},
            },
            scope_key="group:100",
        )
        self.assertEqual(outcome.error_kind, "target_scope_mismatch")
        self.assertEqual(outcome.extra["actual_target_id"], 99999)
        self.assertEqual(bot.calls, [])

    async def test_user_id_mismatch_is_target_scope_mismatch(self) -> None:
        bot_registry.register(_StubBot())
        outcome = await SendMessageTool().run(
            {
                "content": [{"type": "text", "data": {"text": "hi"}}],
                "target": {"kind": "private", "user_id": 777},
            },
            scope_key="private:555",
        )
        self.assertEqual(outcome.error_kind, "target_scope_mismatch")
        self.assertEqual(outcome.extra["actual_target_id"], 777)

    async def test_missing_group_id_is_invalid_arguments(self) -> None:
        # 结构缺字段（不是"发错会话"）→ 仍是 invalid_arguments。
        bot_registry.register(_StubBot())
        outcome = await SendMessageTool().run(
            {
                "content": [{"type": "text", "data": {"text": "hi"}}],
                "target": {"kind": "group"},
            },
            scope_key="group:100",
        )
        self.assertEqual(outcome.error_kind, "invalid_arguments")
        self.assertEqual(outcome.extra["reason_code"], "missing_required_field")


class SendMessageToolValidationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        bot_registry.clear()

    def tearDown(self) -> None:
        bot_registry.clear()

    async def test_empty_content_invalid_arguments(self) -> None:
        bot_registry.register(_StubBot())
        outcome = await SendMessageTool().run(
            {"content": [], "target": {"kind": "group", "group_id": 100}},
            scope_key="group:100",
        )
        self.assertEqual(outcome.error_kind, "invalid_arguments")
        self.assertEqual(outcome.extra["reason_code"], "content_empty")

    async def test_removed_related_image_hashes_field_is_rejected(self) -> None:
        bot_registry.register(_StubBot())
        outcome = await SendMessageTool().run(
            {
                "content": [{"type": "text", "data": {"text": "hi"}}],
                "target": {"kind": "group", "group_id": 100},
                "related_image_hashes": ["abc123"],
            },
            scope_key="group:100",
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "invalid_arguments")
        self.assertEqual(outcome.extra["field"], "related_image_hashes")
        self.assertEqual(outcome.extra["reason_code"], "unsupported_field")

    async def test_content_all_blank_invalid_arguments(self) -> None:
        bot_registry.register(_StubBot())
        outcome = await SendMessageTool().run(
            {
                "content": [{"type": "text", "data": {"text": "   "}}],
                "target": {"kind": "group", "group_id": 100},
            },
            scope_key="group:100",
        )
        self.assertEqual(outcome.error_kind, "invalid_arguments")
        self.assertEqual(outcome.extra["reason_code"], "content_all_blank")

    async def test_unknown_segment_type_invalid_arguments(self) -> None:
        bot_registry.register(_StubBot())
        outcome = await SendMessageTool().run(
            {
                "content": [
                    {"type": "text", "data": {"text": "hi"}},
                    {"type": "image", "data": {"file": "x.png"}},
                ],
                "target": {"kind": "group", "group_id": 100},
            },
            scope_key="group:100",
        )
        self.assertEqual(outcome.error_kind, "invalid_arguments")
        self.assertEqual(outcome.extra["reason_code"], "unsupported_segment_type")
        self.assertEqual(outcome.extra["segment_index"], 1)
        self.assertEqual(outcome.extra["segment_type"], "image")

    async def test_reply_segment_not_first_invalid_arguments(self) -> None:
        bot_registry.register(_StubBot())
        outcome = await SendMessageTool().run(
            {
                "content": [
                    {"type": "text", "data": {"text": "hi"}},
                    {"type": "reply", "data": {"id": "M1"}},
                ],
                "target": {"kind": "group", "group_id": 100},
            },
            scope_key="group:100",
        )
        self.assertEqual(outcome.error_kind, "invalid_arguments")
        self.assertEqual(outcome.extra["reason_code"], "reply_segment_not_first")

    async def test_duplicate_reply_segment_invalid_arguments(self) -> None:
        bot_registry.register(_StubBot())
        outcome = await SendMessageTool().run(
            {
                "content": [
                    {"type": "reply", "data": {"id": "M1"}},
                    {"type": "reply", "data": {"id": "M2"}},
                ],
                "target": {"kind": "group", "group_id": 100},
            },
            scope_key="group:100",
        )
        self.assertEqual(outcome.error_kind, "invalid_arguments")
        self.assertEqual(outcome.extra["reason_code"], "duplicate_reply_segment")

    async def test_missing_scope_key_unavailable_in_scope(self) -> None:
        """send_message 有 allowed_scopes=("group","private")：scope_key 缺失时
        enforce_access 的 scope 闸门（execute 第一行）先于 execute 内的
        invalid_arguments 防御检查命中——当前 scope 解析为 None、不在白名单，
        折 tool_unavailable_in_scope（契约 §7.2 错误表，确定性失败）。"""
        bot_registry.register(_StubBot())
        outcome = await SendMessageTool().run(
            {
                "content": [{"type": "text", "data": {"text": "hi"}}],
                "target": {"kind": "group", "group_id": 100},
            },
        )
        self.assertEqual(outcome.error_kind, "tool_unavailable_in_scope")

    async def test_no_bot_available(self) -> None:
        bot_registry.clear()
        outcome = await SendMessageTool().run(
            {
                "content": [{"type": "text", "data": {"text": "hi"}}],
                "target": {"kind": "group", "group_id": 100},
            },
            scope_key="group:100",
        )
        self.assertEqual(outcome.error_kind, "no_bot_available")

    async def test_napcat_failure_is_upstream_action_failed(self) -> None:
        bot_registry.register(
            _StubBot(
                raise_exc=_FakeActionFailed(
                    1404, "发送失败：群不存在", stream="normal-action"
                )
            )
        )
        outcome = await SendMessageTool().run(
            {
                "content": [{"type": "text", "data": {"text": "hi"}}],
                "target": {"kind": "group", "group_id": 100},
            },
            scope_key="group:100",
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "upstream_action_failed")
        self.assertEqual(outcome.extra["retcode"], 1404)
        self.assertEqual(outcome.extra["action"], "send_group_msg")
        self.assertEqual(outcome.extra["upstream_wording"], "发送失败：群不存在")
        self.assertEqual(outcome.extra["stream"], "normal-action")
        self.assertIn("发送失败：群不存在", outcome.error_message)

    async def test_ok_but_no_message_id_is_upstream_action_failed(self) -> None:
        # §8.3：上游 status=ok 但没回 message_id → 不算成功。
        bot_registry.register(_StubBot(message_id=None))
        outcome = await SendMessageTool().run(
            {
                "content": [{"type": "text", "data": {"text": "hi"}}],
                "target": {"kind": "group", "group_id": 100},
            },
            scope_key="group:100",
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "upstream_action_failed")
        self.assertEqual(outcome.extra["reason_code"], "missing_message_id")
        self.assertEqual(outcome.extra["retcode"], 0)


class SendMessageToolMetadataTests(unittest.TestCase):
    def test_name_and_schema(self) -> None:
        self.assertEqual(SendMessageTool.name, "send_message")
        required = SendMessageTool.arguments_schema["required"]
        self.assertIn("content", required)
        self.assertIn("target", required)
        self.assertNotIn(
            "related_image_hashes",
            SendMessageTool.arguments_schema["properties"],
        )

    def test_usage_prompt_loaded_from_sibling_md(self) -> None:
        self.assertIn(
            "your one and only way to speak", SendMessageTool.usage_prompt
        )

    def test_usage_prompt_teaches_plain_text_default(self) -> None:
        """去 reply 偏置（2026-07-01）：usage 文档必须教"默认纯文本、reply 段
        只在明确要引用那条消息时才用"，不得再把引用回复当成每条消息的标配。"""
        doc = SendMessageTool.usage_prompt
        self.assertIn("default message is just plain", doc)
        self.assertIn("optional, NOT the default", doc)

    def test_usage_prompt_teaches_complete_means_already_said(self) -> None:
        """两态语义：文档必须教 status="complete" 带 <result> = 已发出，
        绝不复读。"""
        doc = SendMessageTool.usage_prompt
        self.assertIn('status="complete"', doc)
        self.assertIn("never something to re-send", doc)

    def test_description_uses_two_state_semantics(self) -> None:
        """description 直接进 tool catalog（模型每 tick 都读）：必须与两态
        语义一致，不得残留旧三态 succeeded/failed 文案污染 catalog。"""
        desc = SendMessageTool.description
        self.assertIn('status="complete"', desc)
        self.assertNotIn("succeeded", desc)
        self.assertNotIn("a failed tool-call", desc)


if __name__ == "__main__":
    unittest.main()

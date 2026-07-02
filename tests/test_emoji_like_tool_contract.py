"""Contract tests for EmojiLikeTool（给消息贴 QQ 表情回应）。

照 test_kick_tool_contract 的"消息动作工具"测试范式：stub Bot 注册进
bot_registry，验证 run() 把 message_id / emoji_id / set 翻成 napcat
set_msg_emoji_like 调用——只凭 message_id、**不传 group_id**，且传给 napcat
的参数名就是 set；非 group scope / 缺参 / 无 bot 各自返回结构化失败 outcome（outcome.error_kind）。
"""

from __future__ import annotations

import unittest
from typing import Any

from qqbot.core.permissions import PermissionTier
from qqbot.services.agent_loop import bot_registry
from qqbot.services.agent_loop.tools.emoji_like import EmojiLikeTool


class _FakeActionFailed(Exception):
    """模拟 nonebot OneBot v11 ActionFailed：完整响应挂在 .info（含 retcode /
    wording）。call_action 据此折成 upstream_action_failed，无需真 import nonebot。"""

    def __init__(self, retcode: int, wording: str) -> None:
        super().__init__(f"ActionFailed: retcode={retcode}")
        self.info = {"status": "failed", "retcode": retcode, "wording": wording}


class _StubBot:
    def __init__(self, self_id: str = "10001", raise_exc: Exception | None = None) -> None:
        self.self_id = self_id
        self.calls: list[tuple[str, dict]] = []
        self._raise = raise_exc

    async def set_msg_emoji_like(self, **kwargs: Any) -> dict:
        self.calls.append(("set_msg_emoji_like", kwargs))
        if self._raise is not None:
            raise self._raise
        return {}


class EmojiLikeToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        bot_registry.clear()

    def tearDown(self) -> None:
        bot_registry.clear()

    async def test_emoji_like_happy_path(self) -> None:
        bot = _StubBot()
        bot_registry.register(bot)
        outcome = await EmojiLikeTool().run(
            {"message_id": 555, "emoji_id": 128},
            scope_key="group:100",
        )
        self.assertEqual(len(bot.calls), 1)
        method, kwargs = bot.calls[0]
        self.assertEqual(method, "set_msg_emoji_like")
        self.assertEqual(kwargs["message_id"], 555)
        # emoji_id 统一转字符串。
        self.assertEqual(kwargs["emoji_id"], "128")
        # 传给 napcat 的参数名就是 set（不是 set_flag）。
        self.assertIn("set", kwargs)
        self.assertTrue(kwargs["set"])
        # 表情回应只凭 message_id —— 不把 group_id 传给 napcat。
        self.assertNotIn("group_id", kwargs)
        # 结构化输出：succeeded → result
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.result["message_id"], 555)
        self.assertEqual(outcome.result["emoji_id"], "128")
        self.assertTrue(outcome.result["set"])

    async def test_set_false_removes_reaction(self) -> None:
        bot = _StubBot()
        bot_registry.register(bot)
        outcome = await EmojiLikeTool().run(
            {"message_id": 555, "emoji_id": "76", "set": False},
            scope_key="group:100",
        )
        kwargs = bot.calls[0][1]
        self.assertFalse(kwargs["set"])
        self.assertEqual(kwargs["emoji_id"], "76")
        self.assertFalse(outcome.result["set"])

    async def test_set_defaults_to_true(self) -> None:
        bot = _StubBot()
        bot_registry.register(bot)
        await EmojiLikeTool().run(
            {"message_id": 555, "emoji_id": "76"}, scope_key="group:100"
        )
        self.assertTrue(bot.calls[0][1]["set"])

    async def test_message_id_as_string_coerced(self) -> None:
        bot = _StubBot()
        bot_registry.register(bot)
        await EmojiLikeTool().run(
            {"message_id": "555", "emoji_id": "76"}, scope_key="group:100"
        )
        self.assertEqual(bot.calls[0][1]["message_id"], 555)

    async def test_non_group_scope_raises(self) -> None:
        # enforce_scope 在 run() 第一行（enforce_access）先于一切拦下越 scope 调用。
        bot_registry.register(_StubBot())
        outcome = await EmojiLikeTool().run(
            {"message_id": 1, "emoji_id": "76"}, scope_key="system"
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "tool_unavailable_in_scope")

    async def test_missing_message_id_raises(self) -> None:
        bot_registry.register(_StubBot())
        outcome = await EmojiLikeTool().run(
            {"emoji_id": "76"}, scope_key="group:100"
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "invalid_arguments")

    async def test_missing_emoji_id_raises(self) -> None:
        bot_registry.register(_StubBot())
        outcome = await EmojiLikeTool().run(
            {"message_id": 555}, scope_key="group:100"
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "invalid_arguments")

    async def test_no_bot_raises(self) -> None:
        bot_registry.clear()
        outcome = await EmojiLikeTool().run(
            {"message_id": 1, "emoji_id": "76"}, scope_key="group:100"
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "no_bot_available")

    async def test_napcat_failure_is_upstream_action_failed(self) -> None:
        # napcat 返回失败（表情 id 非法等）→ ActionFailed 冒泡，call_action 折成
        # upstream_action_failed，带 retcode + wording（人类原因）。
        bot_registry.register(
            _StubBot(raise_exc=_FakeActionFailed(1404, "表情ID非法"))
        )
        outcome = await EmojiLikeTool().run(
            {"message_id": 1, "emoji_id": "76"}, scope_key="group:100"
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "upstream_action_failed")
        self.assertEqual(outcome.extra["retcode"], 1404)
        self.assertEqual(outcome.extra["action"], "set_msg_emoji_like")
        self.assertIn("表情ID非法", outcome.error_message)

    def test_metadata(self) -> None:
        self.assertEqual(EmojiLikeTool.name, "emoji_like")
        self.assertEqual(EmojiLikeTool.allowed_scopes, ("group",))
        # emoji_like 不显式声明，沿用 BaseTool 默认 GUEST。
        self.assertEqual(
            EmojiLikeTool.required_permission, PermissionTier.GUEST
        )
        # 非敏感工具：required_bot_role 不设，沿用默认 None（贴表情回应轻量，
        # bot 无需群管理员）。
        self.assertIsNone(getattr(EmojiLikeTool, "required_bot_role", None))

    def test_usage_md_loaded(self) -> None:
        self.assertIn("set_msg_emoji_like", EmojiLikeTool.usage_prompt)


if __name__ == "__main__":
    unittest.main()

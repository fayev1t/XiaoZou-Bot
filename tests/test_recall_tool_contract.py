"""Contract tests for RecallTool（撤回群消息）。

照 test_kick_tool_contract 的"消息动作工具"测试范式：stub Bot 注册进
bot_registry，验证 run() 把 message_id 翻译成 napcat delete_msg 调用——撤
消息只凭 message_id、**不传 group_id**；非 group scope / 缺参 / 无 bot
各自返回结构化失败 outcome（outcome.error_kind）。
"""

from __future__ import annotations

import unittest
from typing import Any

from qqbot.core.permissions import PermissionTier
from qqbot.services.agent_loop import bot_registry
from qqbot.services.agent_loop.tools.recall import RecallTool


class _FakeActionFailed(Exception):
    """模拟 nonebot OneBot v11 ActionFailed：完整响应挂在 .info（含 retcode /
    wording）。call_action 据此折成 upstream_action_failed，无需真 import nonebot。"""

    def __init__(self, retcode: int, wording: str) -> None:
        super().__init__(f"ActionFailed: retcode={retcode}")
        self.info = {"status": "failed", "retcode": retcode, "wording": wording}


class _StubBot:
    def __init__(
        self,
        self_id: str = "10001",
        raise_exc: Exception | None = None,
        messages: dict[int, dict] | None = None,
    ) -> None:
        self.self_id = self_id
        self.calls: list[tuple[str, dict]] = []
        self._raise = raise_exc
        # message_id → get_msg 返回体（含 sender.user_id / sender.role），供细粒度
        # "撤自己 vs 撤别人"前置判定用。未配置的 message_id 返回 {} → 作者未知 →
        # 前置判定跳过（交 napcat），旧测试行为不变。
        self._messages = messages or {}

    async def delete_msg(self, **kwargs: Any) -> dict:
        self.calls.append(("delete_msg", kwargs))
        if self._raise is not None:
            raise self._raise
        return {}

    async def get_msg(self, *, message_id: int) -> dict:
        return self._messages.get(int(message_id), {})


class RecallToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        bot_registry.clear()

    def tearDown(self) -> None:
        bot_registry.clear()

    async def test_recall_happy_path(self) -> None:
        bot = _StubBot()
        bot_registry.register(bot)
        outcome = await RecallTool().run(
            {"message_id": 555},
            scope_key="group:100",
        )
        self.assertEqual(len(bot.calls), 1)
        method, kwargs = bot.calls[0]
        self.assertEqual(method, "delete_msg")
        self.assertEqual(kwargs["message_id"], 555)
        # 撤消息只凭 message_id —— 不把 group_id 传给 napcat。
        self.assertNotIn("group_id", kwargs)
        # 结构化输出：succeeded → result
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.result["message_id"], 555)

    async def test_message_id_as_string_coerced(self) -> None:
        bot = _StubBot()
        bot_registry.register(bot)
        await RecallTool().run({"message_id": "555"}, scope_key="group:100")
        self.assertEqual(bot.calls[0][1]["message_id"], 555)

    async def test_non_group_scope_raises(self) -> None:
        # enforce_scope 在 run() 第一行（enforce_access）先于一切拦下越 scope 调用。
        bot_registry.register(_StubBot())
        outcome = await RecallTool().run({"message_id": 1}, scope_key="system")
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "tool_unavailable_in_scope")

    async def test_missing_message_id_raises(self) -> None:
        bot_registry.register(_StubBot())
        outcome = await RecallTool().run({}, scope_key="group:100")
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "invalid_arguments")

    async def test_no_bot_raises(self) -> None:
        bot_registry.clear()
        outcome = await RecallTool().run({"message_id": 1}, scope_key="group:100")
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "no_bot_available")

    async def test_napcat_failure_is_upstream_action_failed(self) -> None:
        # napcat 返回失败（消息不存在 / bot 无权撤别人消息等）→ ActionFailed 冒泡，
        # call_action 折成 upstream_action_failed，带 retcode + wording（人类原因）。
        bot_registry.register(
            _StubBot(raise_exc=_FakeActionFailed(1404, "消息不存在"))
        )
        outcome = await RecallTool().run({"message_id": 1}, scope_key="group:100")
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "upstream_action_failed")
        self.assertEqual(outcome.extra["retcode"], 1404)
        self.assertEqual(outcome.extra["action"], "delete_msg")
        self.assertIn("消息不存在", outcome.error_message)

    async def test_recall_own_message_allowed_without_role(self) -> None:
        # 撤 bot 自己发的消息：作者 == self_id → 无需任何角色，放行。
        bot = _StubBot(
            self_id="10001",
            messages={555: {"sender": {"user_id": 10001, "role": "member"}}},
        )
        bot_registry.register(bot)
        outcome = await RecallTool().run(
            {"message_id": 555}, scope_key="group:100", bot_role="member"
        )
        self.assertTrue(outcome.ok)
        self.assertEqual(len(bot.calls), 1)
        self.assertEqual(bot.calls[0][0], "delete_msg")

    async def test_recall_others_message_requires_bot_admin(self) -> None:
        # 撤别人的消息、bot 只是 member → 前置拦下 permission_denied_bot_role
        # （required_bot_role=admin），不发 delete_msg。
        bot = _StubBot(
            self_id="10001",
            messages={555: {"sender": {"user_id": 222, "role": "member"}}},
        )
        bot_registry.register(bot)
        outcome = await RecallTool().run(
            {"message_id": 555}, scope_key="group:100", bot_role="member"
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "permission_denied_bot_role")
        self.assertEqual(outcome.extra["required_bot_role"], "admin")
        self.assertEqual(len(bot.calls), 0)

    async def test_recall_others_message_admin_ok(self) -> None:
        # bot 是 admin、作者是 member → 放行。
        bot = _StubBot(
            self_id="10001",
            messages={555: {"sender": {"user_id": 222, "role": "member"}}},
        )
        bot_registry.register(bot)
        outcome = await RecallTool().run(
            {"message_id": 555}, scope_key="group:100", bot_role="admin"
        )
        self.assertTrue(outcome.ok)
        self.assertEqual(len(bot.calls), 1)

    async def test_recall_higher_role_message_denied(self) -> None:
        # bot 是 admin、作者是群主 → 撤不了（need owner），不发 delete_msg。
        bot = _StubBot(
            self_id="10001",
            messages={555: {"sender": {"user_id": 222, "role": "owner"}}},
        )
        bot_registry.register(bot)
        outcome = await RecallTool().run(
            {"message_id": 555}, scope_key="group:100", bot_role="admin"
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "permission_denied_bot_role")
        self.assertEqual(outcome.extra["required_bot_role"], "owner")
        self.assertEqual(outcome.extra["target_role"], "owner")
        self.assertEqual(len(bot.calls), 0)

    async def test_recall_unknown_author_defers_to_napcat(self) -> None:
        # get_msg 查不到作者（消息未配置）→ 不前置拦，照常发 delete_msg（交 napcat）。
        bot = _StubBot(self_id="10001")
        bot_registry.register(bot)
        outcome = await RecallTool().run(
            {"message_id": 555}, scope_key="group:100", bot_role="member"
        )
        self.assertTrue(outcome.ok)
        self.assertEqual(len(bot.calls), 1)

    def test_metadata(self) -> None:
        self.assertEqual(RecallTool.name, "recall")
        self.assertEqual(RecallTool.allowed_scopes, ("group",))
        # recall 不显式声明，沿用 BaseTool 默认 GUEST。
        self.assertEqual(RecallTool.required_permission, PermissionTier.GUEST)
        # 非敏感工具：required_bot_role 不设，沿用默认 None（撤别人的消息
        # 由 napcat 把关，不在工具层静态门禁）。
        self.assertIsNone(getattr(RecallTool, "required_bot_role", None))

    def test_usage_md_loaded(self) -> None:
        self.assertIn("delete_msg", RecallTool.usage_prompt)


if __name__ == "__main__":
    unittest.main()

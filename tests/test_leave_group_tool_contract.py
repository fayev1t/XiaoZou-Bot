"""Contract tests for LeaveGroupTool（退群 / 解散群，高危）。

照 test_kick_tool_contract.py 的范式：stub Bot 注册进 bot_registry，验证 run()
把 group_id（从 scope_key 注入）+ arguments 正确翻译成 napcat action 调用；
非 group scope / 无 bot 各自 raise。

权限：run() 第一行 self.enforce_access(context) 判发起人 tier；leave_group 不要求
bot 自身角色（required_bot_role=None —— 自己退群不需要 bot 是管理员），所以只补
一个 tier 不足被拒样例，不测 bot 角色。正常路径注入 _OK_CTX（SYSTEM_ADMIN +
owner，恒放行）。漏判由元测试统一兜底。
"""

from __future__ import annotations

import unittest
from typing import Any

from qqbot.core.permissions import PermissionTier
from qqbot.services.agent_loop import bot_registry
from qqbot.services.agent_loop.tools.leave_group import LeaveGroupTool

# 足够通过 enforce_access 的上下文：发起人 SYSTEM_ADMIN + bot 是群主 → 恒放行。
_OK_CTX = {"triggered_by_user_tier": "SYSTEM_ADMIN", "bot_role": "owner"}


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

    async def set_group_leave(self, **kwargs: Any) -> dict:
        self.calls.append(("set_group_leave", kwargs))
        if self._raise is not None:
            raise self._raise
        return {}


class LeaveGroupToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        bot_registry.clear()

    def tearDown(self) -> None:
        bot_registry.clear()

    async def test_leave_group_happy_path(self) -> None:
        bot = _StubBot()
        bot_registry.register(bot)
        outcome = await LeaveGroupTool().run(
            {"is_dismiss": True},
            scope_key="group:100",
            **_OK_CTX,
        )
        self.assertEqual(len(bot.calls), 1)
        method, kwargs = bot.calls[0]
        self.assertEqual(method, "set_group_leave")
        self.assertEqual(kwargs["group_id"], 100)
        self.assertTrue(kwargs["is_dismiss"])
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.result["group_id"], 100)
        self.assertTrue(outcome.result["is_dismiss"])

    async def test_is_dismiss_defaults_false(self) -> None:
        bot = _StubBot()
        bot_registry.register(bot)
        outcome = await LeaveGroupTool().run(
            {}, scope_key="group:100", **_OK_CTX
        )
        self.assertFalse(bot.calls[0][1]["is_dismiss"])
        self.assertFalse(outcome.result["is_dismiss"])

    async def test_non_group_scope_raises(self) -> None:
        bot_registry.register(_StubBot())
        outcome = await LeaveGroupTool().run({}, scope_key="system", **_OK_CTX)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "tool_unavailable_in_scope")

    async def test_no_bot_raises(self) -> None:
        bot_registry.clear()
        outcome = await LeaveGroupTool().run({}, scope_key="group:100", **_OK_CTX)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "no_bot_available")

    async def test_napcat_failure_is_upstream_action_failed(self) -> None:
        # napcat 返回失败（bot 实际无权 / 目标不存在等）→ ActionFailed 冒泡，
        # call_action 折成 upstream_action_failed，带 retcode + wording（人类原因）。
        bot_registry.register(
            _StubBot(raise_exc=_FakeActionFailed(1404, "群成员不存在"))
        )
        outcome = await LeaveGroupTool().run({}, scope_key="group:100", **_OK_CTX)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "upstream_action_failed")
        self.assertEqual(outcome.extra["retcode"], 1404)
        self.assertEqual(outcome.extra["action"], "set_group_leave")
        self.assertIn("群成员不存在", outcome.error_message)

    async def test_insufficient_user_tier_raises(self) -> None:
        # 发起人 tier 不足（GUEST < OWNER）→ enforce_access 第一行就拦。
        # leave_group required_bot_role=None，bot 角色不影响它，故不测 bot 角色。
        bot_registry.register(_StubBot())
        outcome = await LeaveGroupTool().run(
            {},
            scope_key="group:100",
            triggered_by_user_tier="GUEST",
            bot_role="owner",
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "permission_denied_user_tier")

    async def test_dismiss_requires_bot_owner(self) -> None:
        # 细粒度前置判定：is_dismiss=true 解散整个群是群主专属；bot 只是 admin →
        # 前置拦下 permission_denied_bot_role（required_bot_role=owner），不发 napcat。
        bot = _StubBot()
        bot_registry.register(bot)
        outcome = await LeaveGroupTool().run(
            {"is_dismiss": True},
            scope_key="group:100",
            triggered_by_user_tier="SYSTEM_ADMIN",
            bot_role="admin",
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "permission_denied_bot_role")
        self.assertEqual(outcome.extra["required_bot_role"], "owner")
        self.assertEqual(len(bot.calls), 0)

    async def test_string_is_dismiss_false_coerced_to_plain_leave(self) -> None:
        # 回归：is_dismiss 传字符串 "false" —— bool("false") 会误判 True → 走"解散群"
        # 分支（需群主），member bot 会被错误拦下。coerce_bool 须转成 False → 普通退群。
        bot = _StubBot()
        bot_registry.register(bot)
        outcome = await LeaveGroupTool().run(
            {"is_dismiss": "false"},
            scope_key="group:100",
            triggered_by_user_tier="SYSTEM_ADMIN",
            bot_role="member",
        )
        self.assertTrue(outcome.ok, outcome)
        self.assertEqual(len(bot.calls), 1)
        self.assertFalse(bot.calls[0][1]["is_dismiss"])

    async def test_invalid_is_dismiss_returns_invalid_arguments(self) -> None:
        bot = _StubBot()
        bot_registry.register(bot)
        outcome = await LeaveGroupTool().run(
            {"is_dismiss": "maybe"},
            scope_key="group:100",
            triggered_by_user_tier="SYSTEM_ADMIN",
            bot_role="owner",
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "invalid_arguments")
        self.assertEqual(len(bot.calls), 0)

    async def test_plain_leave_allowed_without_bot_role(self) -> None:
        # 普通退群（is_dismiss=false）不需要任何 bot 角色：bot 只是 member 也放行。
        bot = _StubBot()
        bot_registry.register(bot)
        outcome = await LeaveGroupTool().run(
            {},
            scope_key="group:100",
            triggered_by_user_tier="SYSTEM_ADMIN",
            bot_role="member",
        )
        self.assertTrue(outcome.ok)
        self.assertEqual(len(bot.calls), 1)
        self.assertFalse(bot.calls[0][1]["is_dismiss"])

    def test_metadata(self) -> None:
        self.assertEqual(LeaveGroupTool.name, "leave_group")
        self.assertEqual(LeaveGroupTool.allowed_scopes, ("group",))
        self.assertEqual(
            LeaveGroupTool.required_permission, PermissionTier.OWNER
        )
        # 自己退群不需 bot 是管理员 → 不设 required_bot_role（保持 None）。
        self.assertIsNone(getattr(LeaveGroupTool, "required_bot_role", None))

    def test_usage_md_loaded(self) -> None:
        self.assertIn("set_group_leave", LeaveGroupTool.usage_prompt)


if __name__ == "__main__":
    unittest.main()

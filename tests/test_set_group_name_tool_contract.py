"""Contract tests for SetGroupNameTool（修改群名称）。

照 test_kick_tool_contract.py 的范式：stub Bot 注册进 bot_registry，验证 run()
把 group_id（从 scope_key 注入）+ arguments 正确翻译成 napcat action 调用；
注意 napcat 参数名是 group_name；非 group scope / 缺必填参 / 无 bot 各自 raise。

权限：run() 第一行 self.enforce_access(context) 判发起人 tier + bot 自身角色，
所以正常路径测试都注入足够上下文（_OK_CTX = SYSTEM_ADMIN + owner，恒放行）；
另补 tier 不足、bot 角色不足两个被拒样例。漏判由元测试统一兜底。
"""

from __future__ import annotations

import unittest
from typing import Any

from qqbot.core.permissions import PermissionTier
from qqbot.services.agent_loop import bot_registry
from qqbot.services.agent_loop.tools.set_group_name import SetGroupNameTool

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

    async def set_group_name(self, **kwargs: Any) -> dict:
        self.calls.append(("set_group_name", kwargs))
        if self._raise is not None:
            raise self._raise
        return {}


class SetGroupNameToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        bot_registry.clear()

    def tearDown(self) -> None:
        bot_registry.clear()

    async def test_set_group_name_happy_path(self) -> None:
        bot = _StubBot()
        bot_registry.register(bot)
        outcome = await SetGroupNameTool().run(
            {"name": "新群名"},
            scope_key="group:100",
            **_OK_CTX,
        )
        self.assertEqual(len(bot.calls), 1)
        method, kwargs = bot.calls[0]
        self.assertEqual(method, "set_group_name")
        self.assertEqual(kwargs["group_id"], 100)
        # napcat 参数名是 group_name（不是 name）。
        self.assertEqual(kwargs["group_name"], "新群名")
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.result["group_id"], 100)
        self.assertEqual(outcome.result["group_name"], "新群名")

    async def test_non_group_scope_raises(self) -> None:
        bot_registry.register(_StubBot())
        outcome = await SetGroupNameTool().run(
            {"name": "x"}, scope_key="system", **_OK_CTX
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "tool_unavailable_in_scope")

    async def test_missing_name_raises(self) -> None:
        bot_registry.register(_StubBot())
        outcome = await SetGroupNameTool().run({}, scope_key="group:100", **_OK_CTX)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "invalid_arguments")

    async def test_empty_name_raises(self) -> None:
        bot_registry.register(_StubBot())
        outcome = await SetGroupNameTool().run(
            {"name": "   "}, scope_key="group:100", **_OK_CTX
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "invalid_arguments")

    async def test_no_bot_raises(self) -> None:
        bot_registry.clear()
        outcome = await SetGroupNameTool().run(
            {"name": "x"}, scope_key="group:100", **_OK_CTX
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "no_bot_available")

    async def test_napcat_failure_is_upstream_action_failed(self) -> None:
        # napcat 返回失败（bot 实际无权 / 目标不存在等）→ ActionFailed 冒泡，
        # call_action 折成 upstream_action_failed，带 retcode + wording（人类原因）。
        bot_registry.register(
            _StubBot(raise_exc=_FakeActionFailed(1404, "群成员不存在"))
        )
        outcome = await SetGroupNameTool().run(
            {"name": "x"}, scope_key="group:100", **_OK_CTX
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "upstream_action_failed")
        self.assertEqual(outcome.extra["retcode"], 1404)
        self.assertEqual(outcome.extra["action"], "set_group_name")
        self.assertIn("群成员不存在", outcome.error_message)

    async def test_insufficient_user_tier_raises(self) -> None:
        # 发起人 tier 不足（GUEST < OWNER）→ enforce_access 第一行就拦
        bot_registry.register(_StubBot())
        outcome = await SetGroupNameTool().run(
            {"name": "x"},
            scope_key="group:100",
            triggered_by_user_tier="GUEST",
            bot_role="owner",
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "permission_denied_user_tier")

    async def test_bot_not_admin_raises(self) -> None:
        # bot 自己不是管理员（member < admin）→ enforce_access 拦
        bot_registry.register(_StubBot())
        outcome = await SetGroupNameTool().run(
            {"name": "x"},
            scope_key="group:100",
            triggered_by_user_tier="SYSTEM_ADMIN",
            bot_role="member",
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "permission_denied_bot_role")

    def test_metadata(self) -> None:
        self.assertEqual(SetGroupNameTool.name, "set_group_name")
        self.assertEqual(SetGroupNameTool.allowed_scopes, ("group",))
        self.assertEqual(
            SetGroupNameTool.required_permission, PermissionTier.OWNER
        )
        self.assertEqual(SetGroupNameTool.required_bot_role, "admin")

    def test_usage_md_loaded(self) -> None:
        self.assertIn("set_group_name", SetGroupNameTool.usage_prompt)


if __name__ == "__main__":
    unittest.main()

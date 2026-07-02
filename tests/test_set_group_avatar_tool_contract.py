"""Contract tests for SetGroupAvatarTool（设置群头像）。

照 test_kick_tool_contract.py 的范式：stub Bot 注册进 bot_registry，验证 run()
把 group_id（从 scope_key 注入）+ arguments 正确翻译成 napcat action 调用；
非 group scope / 缺必填参 / 无 bot 各自 raise。

权限：run() 第一行 self.enforce_access(context) 判发起人 tier + bot 自身角色，
所以正常路径测试都注入足够上下文（_OK_CTX = SYSTEM_ADMIN + owner，恒放行）；
另补 tier 不足、bot 角色不足两个被拒样例。漏判由元测试统一兜底。
"""

from __future__ import annotations

import unittest
from typing import Any

from qqbot.core.permissions import PermissionTier
from qqbot.services.agent_loop import bot_registry
from qqbot.services.agent_loop.tools.set_group_avatar import SetGroupAvatarTool

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

    async def set_group_portrait(self, **kwargs: Any) -> dict:
        self.calls.append(("set_group_portrait", kwargs))
        if self._raise is not None:
            raise self._raise
        return {}


class SetGroupAvatarToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        bot_registry.clear()

    def tearDown(self) -> None:
        bot_registry.clear()

    async def test_set_group_avatar_happy_path(self) -> None:
        bot = _StubBot()
        bot_registry.register(bot)
        outcome = await SetGroupAvatarTool().run(
            {"file": "https://example.com/a.png"},
            scope_key="group:100",
            **_OK_CTX,
        )
        self.assertEqual(len(bot.calls), 1)
        method, kwargs = bot.calls[0]
        self.assertEqual(method, "set_group_portrait")
        self.assertEqual(kwargs["group_id"], 100)
        self.assertEqual(kwargs["file"], "https://example.com/a.png")
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.result["group_id"], 100)

    async def test_non_group_scope_raises(self) -> None:
        bot_registry.register(_StubBot())
        outcome = await SetGroupAvatarTool().run(
            {"file": "x"}, scope_key="system", **_OK_CTX
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "tool_unavailable_in_scope")

    async def test_missing_file_raises(self) -> None:
        bot_registry.register(_StubBot())
        outcome = await SetGroupAvatarTool().run({}, scope_key="group:100", **_OK_CTX)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "invalid_arguments")

    async def test_empty_file_raises(self) -> None:
        bot_registry.register(_StubBot())
        outcome = await SetGroupAvatarTool().run(
            {"file": "  "}, scope_key="group:100", **_OK_CTX
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "invalid_arguments")

    async def test_no_bot_raises(self) -> None:
        bot_registry.clear()
        outcome = await SetGroupAvatarTool().run(
            {"file": "x"}, scope_key="group:100", **_OK_CTX
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "no_bot_available")

    async def test_napcat_failure_is_upstream_action_failed(self) -> None:
        # napcat 返回失败（bot 实际无权 / 目标不存在等）→ ActionFailed 冒泡，
        # call_action 折成 upstream_action_failed，带 retcode + wording（人类原因）。
        bot_registry.register(
            _StubBot(raise_exc=_FakeActionFailed(1404, "群成员不存在"))
        )
        outcome = await SetGroupAvatarTool().run(
            {"file": "x"}, scope_key="group:100", **_OK_CTX
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "upstream_action_failed")
        self.assertEqual(outcome.extra["retcode"], 1404)
        self.assertEqual(outcome.extra["action"], "set_group_portrait")
        self.assertIn("群成员不存在", outcome.error_message)

    async def test_insufficient_user_tier_raises(self) -> None:
        # 发起人 tier 不足（GUEST < OWNER）→ enforce_access 第一行就拦
        bot_registry.register(_StubBot())
        outcome = await SetGroupAvatarTool().run(
            {"file": "x"},
            scope_key="group:100",
            triggered_by_user_tier="GUEST",
            bot_role="owner",
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "permission_denied_user_tier")

    async def test_bot_not_admin_raises(self) -> None:
        # bot 自己不是管理员（member < admin）→ enforce_access 拦
        bot_registry.register(_StubBot())
        outcome = await SetGroupAvatarTool().run(
            {"file": "x"},
            scope_key="group:100",
            triggered_by_user_tier="SYSTEM_ADMIN",
            bot_role="member",
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "permission_denied_bot_role")

    def test_metadata(self) -> None:
        self.assertEqual(SetGroupAvatarTool.name, "set_group_avatar")
        self.assertEqual(SetGroupAvatarTool.allowed_scopes, ("group",))
        self.assertEqual(
            SetGroupAvatarTool.required_permission, PermissionTier.OWNER
        )
        self.assertEqual(SetGroupAvatarTool.required_bot_role, "admin")

    def test_usage_md_loaded(self) -> None:
        self.assertIn("set_group_portrait", SetGroupAvatarTool.usage_prompt)


if __name__ == "__main__":
    unittest.main()

"""Contract tests for GroupNoticeTool（发布群公告）。

同 KickTool 范式：stub Bot 注册进 bot_registry，验证 run() 把 group_id（从
scope_key 注入）+ arguments 正确翻译成 napcat _send_group_notice 调用（带下划
线前缀的扩展 action 走 call_api，带 / 不带 image 两种）；非 group scope / 缺参
/ 无 bot 各自 raise。

权限：工具 run() 第一行 `self.enforce_access(context)` 判发起人 tier + bot 角色，
所以所有"正常路径"测试都要在 context 注入足够的 triggered_by_user_tier + bot_role
（这里用 _OK_CTX = SYSTEM_ADMIN + owner，恒放行）。另补 2 个权限不足被拒的样例；
全工具的漏判由 test_tool_permission_enforcement_contract 元测试统一兜底。
"""

from __future__ import annotations

import unittest
from typing import Any

from qqbot.core.permissions import PermissionTier
from qqbot.services.agent_loop import bot_registry
from qqbot.services.agent_loop.tools.group_notice import GroupNoticeTool

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

    async def call_api(self, action: str, **kwargs: Any) -> dict:
        self.calls.append((action, kwargs))
        if self._raise is not None:
            raise self._raise
        return {}


class GroupNoticeToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        bot_registry.clear()

    def tearDown(self) -> None:
        bot_registry.clear()

    async def test_notice_happy_path_without_image(self) -> None:
        bot = _StubBot()
        bot_registry.register(bot)
        outcome = await GroupNoticeTool().run(
            {"content": "群规更新，请查阅"},
            scope_key="group:100",
            **_OK_CTX,
        )
        self.assertEqual(len(bot.calls), 1)
        action, kwargs = bot.calls[0]
        self.assertEqual(action, "_send_group_notice")
        self.assertEqual(kwargs["group_id"], 100)
        self.assertEqual(kwargs["content"], "群规更新，请查阅")
        self.assertNotIn("image", kwargs)
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.result["group_id"], 100)

    async def test_notice_happy_path_with_image(self) -> None:
        bot = _StubBot()
        bot_registry.register(bot)
        outcome = await GroupNoticeTool().run(
            {"content": "看图", "image": "http://example.com/y.png"},
            scope_key="group:100",
            **_OK_CTX,
        )
        self.assertEqual(len(bot.calls), 1)
        action, kwargs = bot.calls[0]
        self.assertEqual(action, "_send_group_notice")
        self.assertEqual(kwargs["group_id"], 100)
        self.assertEqual(kwargs["content"], "看图")
        self.assertEqual(kwargs["image"], "http://example.com/y.png")
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.result["group_id"], 100)

    async def test_non_group_scope_raises(self) -> None:
        bot_registry.register(_StubBot())
        outcome = await GroupNoticeTool().run(
            {"content": "x"}, scope_key="system", **_OK_CTX
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "tool_unavailable_in_scope")

    async def test_missing_content_raises(self) -> None:
        bot_registry.register(_StubBot())
        outcome = await GroupNoticeTool().run({}, scope_key="group:100", **_OK_CTX)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "invalid_arguments")

    async def test_empty_content_raises(self) -> None:
        bot_registry.register(_StubBot())
        outcome = await GroupNoticeTool().run(
            {"content": "   "}, scope_key="group:100", **_OK_CTX
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "invalid_arguments")

    async def test_no_bot_raises(self) -> None:
        bot_registry.clear()
        outcome = await GroupNoticeTool().run(
            {"content": "x"}, scope_key="group:100", **_OK_CTX
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "no_bot_available")

    async def test_napcat_failure_is_upstream_action_failed(self) -> None:
        # napcat 返回失败（bot 实际无权 / 目标不存在等）→ ActionFailed 冒泡，
        # call_action 折成 upstream_action_failed，带 retcode + wording（人类原因）。
        # _send_group_notice 无生成同名方法，走 bot.call_api，故 stub 从 call_api 抛。
        bot_registry.register(
            _StubBot(raise_exc=_FakeActionFailed(1404, "群成员不存在"))
        )
        outcome = await GroupNoticeTool().run(
            {"content": "x"}, scope_key="group:100", **_OK_CTX
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "upstream_action_failed")
        self.assertEqual(outcome.extra["retcode"], 1404)
        self.assertEqual(outcome.extra["action"], "_send_group_notice")
        self.assertIn("群成员不存在", outcome.error_message)

    async def test_insufficient_user_tier_raises(self) -> None:
        # 发起人 tier 不足（GUEST < OWNER）→ enforce_access 第一行就拦
        bot_registry.register(_StubBot())
        outcome = await GroupNoticeTool().run(
            {"content": "x"},
            scope_key="group:100",
            triggered_by_user_tier="GUEST",
            bot_role="owner",
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "permission_denied_user_tier")

    async def test_bot_not_admin_raises(self) -> None:
        # bot 自己不是管理员（member < admin）→ enforce_access 拦
        bot_registry.register(_StubBot())
        outcome = await GroupNoticeTool().run(
            {"content": "x"},
            scope_key="group:100",
            triggered_by_user_tier="SYSTEM_ADMIN",
            bot_role="member",
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "permission_denied_bot_role")

    def test_metadata(self) -> None:
        self.assertEqual(GroupNoticeTool.name, "group_notice")
        self.assertEqual(GroupNoticeTool.allowed_scopes, ("group",))
        self.assertEqual(
            GroupNoticeTool.required_permission, PermissionTier.OWNER
        )
        self.assertEqual(GroupNoticeTool.required_bot_role, "admin")

    def test_usage_md_loaded(self) -> None:
        self.assertIn("_send_group_notice", GroupNoticeTool.usage_prompt)


if __name__ == "__main__":
    unittest.main()

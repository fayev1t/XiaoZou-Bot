"""Contract tests for SetCardTool（修改群成员名片）。

同 test_kick_tool_contract：stub Bot 注册进 bot_registry，验证 run() 把
group_id（从 scope_key 注入）+ arguments 翻译成 set_group_card 调用；非 group
scope / 缺参 / 无 bot 各自返回结构化失败 outcome（outcome.error_kind）。

权限：工具 run() 第一行 `await self.enforce_access(context)` 判发起人 tier + bot
角色，所以所有"正常路径"测试都要在 context 注入足够的 triggered_by_user_tier +
bot_role（这里用 _OK_CTX = SYSTEM_ADMIN + owner，恒放行）。另补 2 个权限不足被拒
的样例；全工具的漏判由 test_tool_permission_enforcement_contract 元测试统一兜底。
"""

from __future__ import annotations

import unittest
from typing import Any

from qqbot.core.permissions import PermissionTier
from qqbot.services.agent_loop import bot_registry
from qqbot.services.agent_loop.tools.set_card import SetCardTool

# 足够通过 enforce_access 的上下文：发起人 SYSTEM_ADMIN + bot 是群主 → 恒放行。
_OK_CTX = {"triggered_by_user_tier": "SYSTEM_ADMIN", "bot_role": "owner"}


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
        member_roles: dict[int, str] | None = None,
    ) -> None:
        self.self_id = self_id
        self.calls: list[tuple[str, dict]] = []
        self._raise = raise_exc
        # user_id → role，供细粒度层级前置判定的 fetch_member_role 用。未配置的
        # user 返回 role=None → 前置判定跳过（交 napcat），旧测试行为不变。
        self._member_roles = member_roles or {}

    async def set_group_card(self, **kwargs: Any) -> dict:
        self.calls.append(("set_group_card", kwargs))
        if self._raise is not None:
            raise self._raise
        return {}

    async def get_group_member_info(
        self, *, group_id: int, user_id: int, no_cache: bool = False
    ) -> dict:
        return {"role": self._member_roles.get(int(user_id))}


class SetCardToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        bot_registry.clear()

    def tearDown(self) -> None:
        bot_registry.clear()

    async def test_set_card_happy_path(self) -> None:
        bot = _StubBot()
        bot_registry.register(bot)
        outcome = await SetCardTool().run(
            {"user_id": 222, "card": "新名片"},
            scope_key="group:100",
            **_OK_CTX,
        )
        self.assertEqual(len(bot.calls), 1)
        method, kwargs = bot.calls[0]
        self.assertEqual(method, "set_group_card")
        self.assertEqual(kwargs["group_id"], 100)
        self.assertEqual(kwargs["user_id"], 222)
        self.assertEqual(kwargs["card"], "新名片")
        # 结构化输出：succeeded → result
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.result["group_id"], 100)
        self.assertEqual(outcome.result["user_id"], 222)
        self.assertEqual(outcome.result["card"], "新名片")

    async def test_card_defaults_to_empty(self) -> None:
        bot = _StubBot()
        bot_registry.register(bot)
        outcome = await SetCardTool().run(
            {"user_id": 222}, scope_key="group:100", **_OK_CTX
        )
        self.assertEqual(bot.calls[0][1]["card"], "")
        self.assertEqual(outcome.result["card"], "")

    async def test_user_id_as_string_coerced(self) -> None:
        bot = _StubBot()
        bot_registry.register(bot)
        await SetCardTool().run(
            {"user_id": "222"}, scope_key="group:100", **_OK_CTX
        )
        self.assertEqual(bot.calls[0][1]["user_id"], 222)

    async def test_non_group_scope_raises(self) -> None:
        # enforce_scope 在 run() 第一行（enforce_access）先于一切拦下越 scope 调用。
        bot_registry.register(_StubBot())
        outcome = await SetCardTool().run(
            {"user_id": 1}, scope_key="system", **_OK_CTX
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "tool_unavailable_in_scope")

    async def test_missing_user_id_raises(self) -> None:
        bot_registry.register(_StubBot())
        outcome = await SetCardTool().run({}, scope_key="group:100", **_OK_CTX)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "invalid_arguments")

    async def test_no_bot_raises(self) -> None:
        bot_registry.clear()
        outcome = await SetCardTool().run(
            {"user_id": 1}, scope_key="group:100", **_OK_CTX
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "no_bot_available")

    async def test_napcat_failure_is_upstream_action_failed(self) -> None:
        # napcat 返回失败（bot 实际无权等）→ ActionFailed 冒泡，call_action 折成
        # upstream_action_failed，带 retcode + wording（人类原因）。
        bot_registry.register(
            _StubBot(raise_exc=_FakeActionFailed(1404, "权限不足"))
        )
        outcome = await SetCardTool().run(
            {"user_id": 1}, scope_key="group:100", **_OK_CTX
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "upstream_action_failed")
        self.assertEqual(outcome.extra["retcode"], 1404)
        self.assertEqual(outcome.extra["action"], "set_group_card")
        self.assertIn("权限不足", outcome.error_message)

    async def test_insufficient_user_tier_raises(self) -> None:
        # 发起人 tier 不足（GUEST < ADMIN）→ enforce_access 第一行就拦
        bot_registry.register(_StubBot())
        outcome = await SetCardTool().run(
            {"user_id": 1},
            scope_key="group:100",
            triggered_by_user_tier="GUEST",
            bot_role="owner",
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "permission_denied_user_tier")

    async def test_bot_not_admin_raises(self) -> None:
        # bot 自己不是管理员（member < admin）→ enforce_access 拦
        bot_registry.register(_StubBot())
        outcome = await SetCardTool().run(
            {"user_id": 1},
            scope_key="group:100",
            triggered_by_user_tier="SYSTEM_ADMIN",
            bot_role="member",
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "permission_denied_bot_role")

    async def test_cannot_edit_higher_role_card(self) -> None:
        # bot 是 admin、目标是群主 → 改不了对方名片（need owner），不发 set_group_card。
        bot = _StubBot(self_id="10001", member_roles={222: "owner"})
        bot_registry.register(bot)
        outcome = await SetCardTool().run(
            {"user_id": 222, "card": "x"},
            scope_key="group:100",
            triggered_by_user_tier="SYSTEM_ADMIN",
            bot_role="admin",
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "permission_denied_bot_role")
        self.assertEqual(outcome.extra["target_role"], "owner")
        self.assertEqual(len(bot.calls), 0)

    async def test_edit_own_card_bypasses_hierarchy(self) -> None:
        # 改 bot 自己的名片：target == self_id → 跳过层级判定，即使同角色也放行。
        bot = _StubBot(self_id="10001", member_roles={10001: "admin"})
        bot_registry.register(bot)
        outcome = await SetCardTool().run(
            {"user_id": 10001, "card": "小奏"},
            scope_key="group:100",
            triggered_by_user_tier="SYSTEM_ADMIN",
            bot_role="admin",
        )
        self.assertTrue(outcome.ok)
        self.assertEqual(len(bot.calls), 1)
        self.assertEqual(bot.calls[0][1]["user_id"], 10001)

    def test_metadata(self) -> None:
        self.assertEqual(SetCardTool.name, "set_card")
        self.assertEqual(SetCardTool.allowed_scopes, ("group",))
        self.assertEqual(SetCardTool.required_permission, PermissionTier.ADMIN)
        self.assertEqual(SetCardTool.required_bot_role, "admin")

    def test_usage_md_loaded(self) -> None:
        self.assertIn("set_group_card", SetCardTool.usage_prompt)


if __name__ == "__main__":
    unittest.main()

"""Contract tests for SetEssenceTool（设/取消群精华消息）。

照 test_kick_tool_contract 的"消息动作工具"测试范式：stub Bot 注册进
bot_registry，验证 run() 按 action 把 message_id 翻成 napcat
set_essence_msg / delete_essence_msg——只凭 message_id、**不传 group_id**；
非 group scope / 缺参 / 非法 action / 无 bot 各自返回结构化失败 outcome（outcome.error_kind）。

权限：set_essence 是敏感工具（required_permission=OWNER + required_bot_role=
"admin"），run() 第一行 `await self.enforce_access(context)` 判发起人 tier + bot
角色，所以所有"正常路径"测试都要在 context 注入足够的 triggered_by_user_tier +
bot_role（这里用 _OK_CTX = SYSTEM_ADMIN + owner，恒放行）。另补 2 个权限不足
被拒的样例；全工具的漏判由 test_tool_permission_enforcement_contract 元测试
统一兜底。
"""

from __future__ import annotations

import unittest
from typing import Any

from qqbot.core.permissions import PermissionTier
from qqbot.services.agent_loop import bot_registry
from qqbot.services.agent_loop.tools.set_essence import SetEssenceTool

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

    async def set_essence_msg(self, **kwargs: Any) -> dict:
        self.calls.append(("set_essence_msg", kwargs))
        if self._raise is not None:
            raise self._raise
        return {}

    async def delete_essence_msg(self, **kwargs: Any) -> dict:
        self.calls.append(("delete_essence_msg", kwargs))
        if self._raise is not None:
            raise self._raise
        return {}


class SetEssenceToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        bot_registry.clear()

    def tearDown(self) -> None:
        bot_registry.clear()

    async def test_set_happy_path(self) -> None:
        bot = _StubBot()
        bot_registry.register(bot)
        outcome = await SetEssenceTool().run(
            {"message_id": 555, "action": "set"},
            scope_key="group:100",
            **_OK_CTX,
        )
        self.assertEqual(len(bot.calls), 1)
        method, kwargs = bot.calls[0]
        self.assertEqual(method, "set_essence_msg")
        self.assertEqual(kwargs["message_id"], 555)
        # 精华操作只凭 message_id —— 不把 group_id 传给 napcat。
        self.assertNotIn("group_id", kwargs)
        # 结构化输出：succeeded → result
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.result["message_id"], 555)
        self.assertEqual(outcome.result["action"], "set")

    async def test_action_defaults_to_set(self) -> None:
        bot = _StubBot()
        bot_registry.register(bot)
        outcome = await SetEssenceTool().run(
            {"message_id": 555}, scope_key="group:100", **_OK_CTX
        )
        self.assertEqual(bot.calls[0][0], "set_essence_msg")
        self.assertEqual(outcome.result["action"], "set")

    async def test_delete_happy_path(self) -> None:
        bot = _StubBot()
        bot_registry.register(bot)
        outcome = await SetEssenceTool().run(
            {"message_id": 777, "action": "delete"},
            scope_key="group:100",
            **_OK_CTX,
        )
        method, kwargs = bot.calls[0]
        self.assertEqual(method, "delete_essence_msg")
        self.assertEqual(kwargs["message_id"], 777)
        self.assertNotIn("group_id", kwargs)
        self.assertEqual(outcome.result["action"], "delete")

    async def test_invalid_action_raises(self) -> None:
        bot_registry.register(_StubBot())
        outcome = await SetEssenceTool().run(
            {"message_id": 1, "action": "toggle"},
            scope_key="group:100",
            **_OK_CTX,
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "invalid_arguments")

    async def test_message_id_as_string_coerced(self) -> None:
        bot = _StubBot()
        bot_registry.register(bot)
        await SetEssenceTool().run(
            {"message_id": "555"}, scope_key="group:100", **_OK_CTX
        )
        self.assertEqual(bot.calls[0][1]["message_id"], 555)

    async def test_non_group_scope_raises(self) -> None:
        # enforce_scope 在 run() 第一行（enforce_access）先于一切拦下越 scope 调用。
        bot_registry.register(_StubBot())
        outcome = await SetEssenceTool().run(
            {"message_id": 1}, scope_key="system", **_OK_CTX
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "tool_unavailable_in_scope")

    async def test_missing_message_id_raises(self) -> None:
        bot_registry.register(_StubBot())
        outcome = await SetEssenceTool().run({}, scope_key="group:100", **_OK_CTX)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "invalid_arguments")

    async def test_no_bot_raises(self) -> None:
        bot_registry.clear()
        outcome = await SetEssenceTool().run(
            {"message_id": 1}, scope_key="group:100", **_OK_CTX
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "no_bot_available")

    async def test_napcat_failure_is_upstream_action_failed(self) -> None:
        # napcat 返回失败（bot 实际无权等）→ ActionFailed 冒泡，call_action 折成
        # upstream_action_failed，带 retcode + wording（人类原因）。
        bot_registry.register(
            _StubBot(raise_exc=_FakeActionFailed(1404, "权限不足"))
        )
        outcome = await SetEssenceTool().run(
            {"message_id": 1}, scope_key="group:100", **_OK_CTX
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "upstream_action_failed")
        self.assertEqual(outcome.extra["retcode"], 1404)
        self.assertEqual(outcome.extra["action"], "set_essence_msg")
        self.assertIn("权限不足", outcome.error_message)

    async def test_insufficient_user_tier_raises(self) -> None:
        # 发起人 tier 不足（GUEST < OWNER）→ enforce_access 第一行就拦
        bot_registry.register(_StubBot())
        outcome = await SetEssenceTool().run(
            {"message_id": 1},
            scope_key="group:100",
            triggered_by_user_tier="GUEST",
            bot_role="owner",
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "permission_denied_user_tier")

    async def test_bot_not_admin_raises(self) -> None:
        # bot 自己不是管理员（member < admin）→ enforce_access 拦
        bot_registry.register(_StubBot())
        outcome = await SetEssenceTool().run(
            {"message_id": 1},
            scope_key="group:100",
            triggered_by_user_tier="SYSTEM_ADMIN",
            bot_role="member",
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "permission_denied_bot_role")

    def test_metadata(self) -> None:
        self.assertEqual(SetEssenceTool.name, "set_essence")
        self.assertEqual(SetEssenceTool.allowed_scopes, ("group",))
        self.assertEqual(
            SetEssenceTool.required_permission, PermissionTier.OWNER
        )
        # 敏感工具：set_essence_msg 需要 bot 自己是群管理员。
        self.assertEqual(SetEssenceTool.required_bot_role, "admin")

    def test_usage_md_loaded(self) -> None:
        self.assertIn("set_essence_msg", SetEssenceTool.usage_prompt)


if __name__ == "__main__":
    unittest.main()

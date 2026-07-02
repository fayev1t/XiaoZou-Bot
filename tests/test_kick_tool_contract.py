"""Contract tests for KickTool（群成员踢出）。

"群管理动作工具"的测试范式（**所有同类工具照此写**）：stub Bot 注册进
bot_registry，验证 run() 把 group_id（从 scope_key 注入）+ arguments 翻译成
napcat action。

关键：工具**永不 raise**——`run()` 无论成功失败都返回一个 `ToolOutcome`
（`BaseTool.run` 把 `execute()` 的 raise/return 统一收敛）。所以测试**没有一处
assertRaises**：一律 `outcome = await tool.run(...)` 后断 `outcome.ok` /
`outcome.error_kind` / `outcome.result` / `outcome.extra`。

失败 error_kind 语义集（固定 7 种）：
- 非 group scope → `tool_unavailable_in_scope`（enforce_scope 先于一切）
- 缺参 / 参数非法 → `invalid_arguments`
- 无 bot → `no_bot_available`
- napcat 动作失败（ActionFailed）→ `upstream_action_failed`（带 retcode + wording）
- 发起人 tier 不足 → `permission_denied_user_tier`
- bot 自身角色不足 → `permission_denied_bot_role`

全工具的"漏判权限"由 test_tool_permission_enforcement_contract 元测试统一兜底。
"""

from __future__ import annotations

import unittest
from typing import Any

from qqbot.core.permissions import PermissionTier
from qqbot.services.agent_loop import bot_registry
from qqbot.services.agent_loop.tools.kick import KickTool

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
        # user 返回 role=None → 前置判定跳过（交 napcat），保持旧测试行为不变。
        self._member_roles = member_roles or {}

    async def set_group_kick(self, **kwargs: Any) -> dict:
        self.calls.append(("set_group_kick", kwargs))
        if self._raise is not None:
            raise self._raise
        return {}

    async def get_group_member_info(
        self, *, group_id: int, user_id: int, no_cache: bool = False
    ) -> dict:
        return {"role": self._member_roles.get(int(user_id))}


class KickToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        bot_registry.clear()

    def tearDown(self) -> None:
        bot_registry.clear()

    async def test_kick_happy_path(self) -> None:
        bot = _StubBot()
        bot_registry.register(bot)
        outcome = await KickTool().run(
            {"user_id": 222, "reject_add_request": True},
            scope_key="group:100",
            **_OK_CTX,
        )
        self.assertEqual(len(bot.calls), 1)
        method, kwargs = bot.calls[0]
        self.assertEqual(method, "set_group_kick")
        self.assertEqual(kwargs["group_id"], 100)
        self.assertEqual(kwargs["user_id"], 222)
        self.assertTrue(kwargs["reject_add_request"])
        # 结构化输出：succeeded → result
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.result["group_id"], 100)
        self.assertEqual(outcome.result["user_id"], 222)

    async def test_user_id_as_string_coerced(self) -> None:
        bot = _StubBot()
        bot_registry.register(bot)
        await KickTool().run(
            {"user_id": "222"}, scope_key="group:100", **_OK_CTX
        )
        self.assertEqual(bot.calls[0][1]["user_id"], 222)

    async def test_string_reject_add_request_false_coerced(self) -> None:
        # 回归：reject_add_request 传字符串 "false" 须转成 False（bool("false")误判 True）。
        bot = _StubBot()
        bot_registry.register(bot)
        outcome = await KickTool().run(
            {"user_id": 222, "reject_add_request": "false"},
            scope_key="group:100",
            **_OK_CTX,
        )
        self.assertTrue(outcome.ok, outcome)
        self.assertFalse(bot.calls[0][1]["reject_add_request"])

    async def test_non_group_scope_returns_tool_unavailable(self) -> None:
        # enforce_scope 在 execute() 第一行（enforce_access）先于一切拦下越 scope 调用。
        bot_registry.register(_StubBot())
        outcome = await KickTool().run(
            {"user_id": 1}, scope_key="system", **_OK_CTX
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "tool_unavailable_in_scope")

    async def test_missing_user_id_invalid_arguments(self) -> None:
        bot_registry.register(_StubBot())
        outcome = await KickTool().run({}, scope_key="group:100", **_OK_CTX)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "invalid_arguments")

    async def test_no_bot_available(self) -> None:
        bot_registry.clear()
        outcome = await KickTool().run(
            {"user_id": 1}, scope_key="group:100", **_OK_CTX
        )
        self.assertEqual(outcome.error_kind, "no_bot_available")

    async def test_napcat_failure_is_upstream_action_failed(self) -> None:
        # napcat 返回失败（bot 实际无权 / 目标不存在等）→ ActionFailed，call_action
        # 折成 upstream_action_failed，带 retcode + wording（人类原因）——全在 outcome 里。
        bot_registry.register(
            _StubBot(raise_exc=_FakeActionFailed(1404, "群成员不存在"))
        )
        outcome = await KickTool().run(
            {"user_id": 1}, scope_key="group:100", **_OK_CTX
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "upstream_action_failed")
        self.assertEqual(outcome.extra["retcode"], 1404)
        self.assertEqual(outcome.extra["action"], "set_group_kick")
        self.assertIn("群成员不存在", outcome.error_message)

    async def test_insufficient_user_tier(self) -> None:
        # 发起人 tier 不足（GUEST < ADMIN）→ enforce_access 第一行就拦；结构化
        # error_kind permission_denied_user_tier（附 required/actual tier 在 extra）。
        bot_registry.register(_StubBot())
        outcome = await KickTool().run(
            {"user_id": 1},
            scope_key="group:100",
            triggered_by_user_tier="GUEST",
            bot_role="owner",
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "permission_denied_user_tier")
        self.assertEqual(outcome.extra["required_tier"], "ADMIN")
        self.assertEqual(outcome.extra["actual_tier"], "GUEST")

    async def test_bot_not_admin(self) -> None:
        # bot 自己不是管理员（member < admin）→ permission_denied_bot_role。
        bot_registry.register(_StubBot())
        outcome = await KickTool().run(
            {"user_id": 1},
            scope_key="group:100",
            triggered_by_user_tier="SYSTEM_ADMIN",
            bot_role="member",
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "permission_denied_bot_role")

    async def test_cannot_kick_equal_or_higher_role(self) -> None:
        # 细粒度层级前置判定：bot 是 admin、目标也是 admin → 踢不动（need owner），
        # **不发** set_group_kick（先于 napcat 拦下），带 target_role。
        bot = _StubBot(member_roles={222: "admin"})
        bot_registry.register(bot)
        outcome = await KickTool().run(
            {"user_id": 222},
            scope_key="group:100",
            triggered_by_user_tier="SYSTEM_ADMIN",
            bot_role="admin",
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "permission_denied_bot_role")
        self.assertEqual(outcome.extra["required_bot_role"], "owner")
        self.assertEqual(outcome.extra["target_role"], "admin")
        self.assertEqual(len(bot.calls), 0)

    async def test_can_kick_lower_role(self) -> None:
        # bot 是 admin、目标是 member → 放行。
        bot = _StubBot(member_roles={222: "member"})
        bot_registry.register(bot)
        outcome = await KickTool().run(
            {"user_id": 222},
            scope_key="group:100",
            triggered_by_user_tier="SYSTEM_ADMIN",
            bot_role="admin",
        )
        self.assertTrue(outcome.ok)
        self.assertEqual(len(bot.calls), 1)

    async def test_unknown_target_role_defers_to_napcat(self) -> None:
        # 目标角色查不到（None）→ 不前置拦，照常发 set_group_kick（交 napcat 兜底）。
        # 注：bot 自己(10001)也未配置 → 实时查 bot 角色返回 None → 回退注入的快照 admin。
        bot = _StubBot()  # member_roles 空 → get_group_member_info 返回 role=None
        bot_registry.register(bot)
        outcome = await KickTool().run(
            {"user_id": 999},
            scope_key="group:100",
            triggered_by_user_tier="SYSTEM_ADMIN",
            bot_role="admin",
        )
        self.assertTrue(outcome.ok)
        self.assertEqual(len(bot.calls), 1)

    async def test_live_bot_role_overrides_stale_snapshot_allows(self) -> None:
        # point 2：bot 自身角色以**实时**查为准。快照说 member（过期，sweep 未更新），
        # 实时查 bot 其实已是 admin → 应放行（若只吃快照会假阴性、错误拒绝）。
        bot = _StubBot(member_roles={10001: "admin", 222: "member"})
        bot_registry.register(bot)
        outcome = await KickTool().run(
            {"user_id": 222},
            scope_key="group:100",
            triggered_by_user_tier="SYSTEM_ADMIN",
            bot_role="member",  # 过期快照
        )
        self.assertTrue(outcome.ok, outcome)
        self.assertEqual(len(bot.calls), 1)

    async def test_live_bot_role_overrides_stale_snapshot_denies(self) -> None:
        # 反向：快照说 owner（过期），实时查 bot 只是 member → enforce_bot_admin 实时拒，
        # 不发 napcat。证明实时值双向覆盖快照。
        bot = _StubBot(member_roles={10001: "member"})
        bot_registry.register(bot)
        outcome = await KickTool().run(
            {"user_id": 222},
            scope_key="group:100",
            triggered_by_user_tier="SYSTEM_ADMIN",
            bot_role="owner",  # 过期快照
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "permission_denied_bot_role")
        self.assertEqual(len(bot.calls), 0)

    def test_metadata(self) -> None:
        self.assertEqual(KickTool.name, "kick")
        self.assertEqual(KickTool.allowed_scopes, ("group",))
        self.assertEqual(KickTool.required_permission, PermissionTier.ADMIN)
        self.assertEqual(KickTool.required_bot_role, "admin")

    def test_usage_md_loaded(self) -> None:
        self.assertIn("set_group_kick", KickTool.usage_prompt)


if __name__ == "__main__":
    unittest.main()

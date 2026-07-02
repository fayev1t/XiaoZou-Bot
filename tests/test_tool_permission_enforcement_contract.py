"""Meta contract test：每个工具确实在 run() 内 enforce 了权限（并**返回**失败）。

权限判定下放工具内（BaseTool.enforce_access = enforce_scope + enforce_permission +
enforce_bot_admin，async，**返回** ToolOutcome|None）后，靠每个工具 execute() 第一行
``if fail := await self.enforce_access(context): return fail``。工具**永不 raise**——
run() 无论成功失败都返回一个 ToolOutcome。

本元测试是**防漏判的安全网**：遍历**所有内置工具**（tools.__all__ 里的全部
BaseTool 子类，见 _registry_of_all_builtin_tools）——刻意**不用**
build_default_registry()：它 2026-07-01 起默认只注册 send_message、其余工具整体下架，
但「下架」不等于允许它们的权限闸门退化——重做恢复时若漏了 enforce_access，本网
仍须变红。遍历项：
- required_permission > GUEST 的工具：发起人 GUEST 触发 → outcome.error_kind ==
  permission_denied_user_tier；
- required_bot_role 非 None 的工具：bot 角色不足（member）→ permission_denied_bot_role；
- 两类工具在 SYSTEM_ADMIN + owner 的足够上下文下 outcome 都**不得**是权限/scope 拒绝。
哪个工具忘了在 execute() 首行 enforce_access（或忘了 return fail），这里立刻变红。

不打真 DB / napcat：直接注入 triggered_by_user_tier + bot_role，enforce_permission
走「预置 tier」override 分支（免解析、免 DB）。
"""

from __future__ import annotations

import unittest

from qqbot.core.permissions import PermissionTier
from qqbot.services.agent_loop import tools as tools_pkg
from qqbot.services.agent_loop.tool_registry import (
    BaseTool,
    ToolRegistry,
    get_tool_required_bot_role,
    get_tool_required_permission,
)

# enforce_access 放行后可因缺参 / 缺 bot 失败，但这些都**不是**权限/scope 拒绝。
_DENIALS = {
    "permission_denied_user_tier",
    "permission_denied_bot_role",
    "tool_unavailable_in_scope",
}


def _registry_of_all_builtin_tools() -> ToolRegistry:
    """含**所有内置工具**的 registry —— 本安全网的遍历源。

    校验的是「每个工具都在 execute() 首行 enforce_access 并 return 失败」，与
    build_default_registry() 当前实际注册了哪些工具**无关**：默认注册表 2026-07-01
    起只留 send_message，但下架工具的权限闸门仍须正确。故遍历 tools.__all__ 里的全部
    BaseTool 子类自建 registry，而非用会被裁剪的 build_default_registry()。
    工具无构造依赖，直接实例化即可。"""
    reg = ToolRegistry()
    for attr in tools_pkg.__all__:
        obj = getattr(tools_pkg, attr)
        if (
            isinstance(obj, type)
            and issubclass(obj, BaseTool)
            and obj is not BaseTool
        ):
            reg.register(obj())
    return reg


class ToolPermissionEnforcementMetaTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        # 清空 bot_registry：enforce_bot_admin 现在会 get_any() 实时查 bot 角色，
        # 无 bot 时回退注入的 bot_role 快照——这里靠"无 bot"让判定确定性地走快照，
        # 不受别的测试遗留的 stub Bot 干扰。
        from qqbot.services.agent_loop import bot_registry

        bot_registry.clear()

    def tearDown(self) -> None:
        from qqbot.services.agent_loop import bot_registry

        bot_registry.clear()

    async def test_tier_insufficient_is_rejected(self) -> None:
        reg = _registry_of_all_builtin_tools()
        tier_tools = [
            n
            for n in reg.names()
            if get_tool_required_permission(reg.get(n)) > PermissionTier.GUEST
        ]
        self.assertTrue(tier_tools, "registry has no tier-gated tools")
        for n in tier_tools:
            with self.subTest(tool=n):
                # bot_role 给足，单独暴露 tier 不足
                outcome = await reg.get(n).run(
                    {},
                    scope_key="group:1",
                    triggered_by_user_tier="GUEST",
                    bot_role="owner",
                )
                self.assertFalse(
                    outcome.ok, f"{n} 未在 execute() 内 enforce 发起人 tier"
                )
                self.assertEqual(
                    outcome.error_kind,
                    "permission_denied_user_tier",
                    f"{n} 应因发起人 tier 不足返回 permission_denied_user_tier",
                )

    async def test_bot_role_insufficient_is_rejected(self) -> None:
        reg = _registry_of_all_builtin_tools()
        bot_tools = [
            n
            for n in reg.names()
            if get_tool_required_bot_role(reg.get(n)) is not None
        ]
        self.assertTrue(bot_tools, "registry has no bot-role-gated tools")
        for n in bot_tools:
            with self.subTest(tool=n):
                # tier 给足，单独暴露 bot_role 不足（member < admin/owner）
                outcome = await reg.get(n).run(
                    {},
                    scope_key="group:1",
                    triggered_by_user_tier="SYSTEM_ADMIN",
                    bot_role="member",
                )
                self.assertFalse(
                    outcome.ok, f"{n} 未在 execute() 内 enforce bot 自身角色"
                )
                self.assertEqual(
                    outcome.error_kind,
                    "permission_denied_bot_role",
                    f"{n} 应因 bot 角色不足返回 permission_denied_bot_role",
                )

    async def test_sufficient_access_passes_permission(self) -> None:
        # SYSTEM_ADMIN + owner 足够通过任何工具的 enforce_access；之后会因缺参 /
        # 缺 bot 失败，但 outcome.error_kind **不得**是权限/scope 拒绝（证明放行正确）。
        reg = _registry_of_all_builtin_tools()
        guarded = [
            n
            for n in reg.names()
            if get_tool_required_permission(reg.get(n)) > PermissionTier.GUEST
            or get_tool_required_bot_role(reg.get(n)) is not None
        ]
        for n in guarded:
            with self.subTest(tool=n):
                outcome = await reg.get(n).run(
                    {},
                    scope_key="group:1",
                    triggered_by_user_tier="SYSTEM_ADMIN",
                    bot_role="owner",
                )
                self.assertNotIn(
                    outcome.error_kind,
                    _DENIALS,
                    f"{n} 在 SYSTEM_ADMIN+owner 足够上下文下仍被权限/scope 拒绝",
                )


if __name__ == "__main__":
    unittest.main()

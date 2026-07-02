"""Contract tests for ToolRegistry.

Covers (任务与决策契约 §5.1):
- register / get / names / catalog 基本语义
- 重复 register 同名 → ValueError
- 缺 name 的 tool → ValueError
- catalog 渲染为 LLM 可消费的 [{name, description, arguments_schema,
  required_permission, required_bot_role, require_bot_admin}] 列表
- **`required_bot_role`（"admin"/"owner"/None）是主契约字段**；`require_bot_admin`
  只是向后兼容的旧布尔字段（True 等价 required_bot_role="admin"），仍随 catalog
  透出但不再是首要判据
- 缺 required_permission / required_bot_role 的老 stub 默认 GUEST / None
- 显式标注的 tool 正确透传（含 owner 级）
- in 运算符 / len 实现
"""

from __future__ import annotations

import unittest
from typing import Any

from qqbot.core.permissions import PermissionTier
from qqbot.services.agent_loop.tool_registry import (
    ToolRegistry,
    get_tool_allowed_scopes,
    get_tool_require_bot_admin,
    get_tool_required_bot_role,
    get_tool_required_permission,
)


class _StubTool:
    def __init__(self, name: str, description: str = "d", schema: dict | None = None) -> None:
        self.name = name
        self.description = description
        self.arguments_schema = schema if schema is not None else {"type": "object"}

    async def run(self, arguments: dict) -> Any:
        return {"echo": arguments}


class ToolRegistryContractTest(unittest.TestCase):
    def test_register_and_get(self) -> None:
        reg = ToolRegistry()
        tool = _StubTool("websearch")
        reg.register(tool)
        self.assertIs(reg.get("websearch"), tool)
        self.assertIsNone(reg.get("nope"))

    def test_duplicate_name_raises(self) -> None:
        reg = ToolRegistry()
        reg.register(_StubTool("a"))
        with self.assertRaises(ValueError):
            reg.register(_StubTool("a"))

    def test_missing_name_raises(self) -> None:
        reg = ToolRegistry()

        class _Anon:
            name = ""
            description = "d"
            arguments_schema = {}

            async def run(self, arguments: dict) -> Any:
                return None

        with self.assertRaises(ValueError):
            reg.register(_Anon())

    def test_names_sorted(self) -> None:
        reg = ToolRegistry()
        reg.register(_StubTool("zeta"))
        reg.register(_StubTool("alpha"))
        reg.register(_StubTool("mu"))
        self.assertEqual(reg.names(), ["alpha", "mu", "zeta"])

    def test_catalog_shape(self) -> None:
        reg = ToolRegistry()
        reg.register(
            _StubTool(
                "search",
                description="Search the web",
                schema={"type": "object", "properties": {"q": {"type": "string"}}},
            )
        )
        catalog = reg.catalog()
        self.assertEqual(len(catalog), 1)
        entry = catalog[0]
        self.assertEqual(entry["name"], "search")
        self.assertEqual(entry["description"], "Search the web")
        self.assertEqual(
            entry["arguments_schema"],
            {"type": "object", "properties": {"q": {"type": "string"}}},
        )
        # 老 stub 没标权限属性 → 默认 GUEST / required_bot_role=None / require_bot_admin=False
        self.assertEqual(entry["required_permission"], "GUEST")
        self.assertIsNone(entry["required_bot_role"])
        self.assertFalse(entry["require_bot_admin"])

    def test_catalog_propagates_required_bot_role(self) -> None:
        """主契约：工具用 `required_bot_role`（"admin"/"owner"）声明 bot 角色要求，
        catalog 原样透出该字段。旧布尔 `require_bot_admin` 只反映**旧属性本身**（读
        `require_bot_admin` 特性），**不**从 `required_bot_role` 反推——所以新式声明的
        工具其 `require_bot_admin` 恒为 False，正说明它已是过时字段、表达力不足
        （分不清 admin 与 owner）。"""

        class _AdminTool:
            name = "kick_member"
            description = "Kick a member"
            arguments_schema = {"type": "object"}
            required_permission = PermissionTier.ADMIN
            required_bot_role = "admin"

            async def run(self, arguments: dict, **_: Any) -> Any:
                return {}

        class _OwnerTool:
            name = "dismiss_group"
            description = "Dismiss"
            arguments_schema = {"type": "object"}
            required_permission = PermissionTier.OWNER
            required_bot_role = "owner"

            async def run(self, arguments: dict, **_: Any) -> Any:
                return {}

        reg = ToolRegistry()
        reg.register(_AdminTool())
        reg.register(_OwnerTool())
        by_name = {e["name"]: e for e in reg.catalog()}
        admin = by_name["kick_member"]
        self.assertEqual(admin["required_permission"], "ADMIN")
        self.assertEqual(admin["required_bot_role"], "admin")
        # 新式声明不设旧布尔属性 → 旧字段为 False（与 required_bot_role 独立）。
        self.assertFalse(admin["require_bot_admin"])
        owner = by_name["dismiss_group"]
        self.assertEqual(owner["required_bot_role"], "owner")
        self.assertFalse(owner["require_bot_admin"])

    def test_catalog_legacy_require_bot_admin_maps_to_admin(self) -> None:
        """向后兼容：只标了旧布尔 `require_bot_admin=True`、没标 `required_bot_role`
        的工具，catalog 的 `required_bot_role` 派生为 "admin"。"""

        class _LegacyTool:
            name = "legacy"
            description = "legacy admin tool"
            arguments_schema = {"type": "object"}
            require_bot_admin = True

            async def run(self, arguments: dict, **_: Any) -> Any:
                return {}

        reg = ToolRegistry()
        reg.register(_LegacyTool())
        entry = reg.catalog()[0]
        self.assertEqual(entry["required_bot_role"], "admin")
        self.assertTrue(entry["require_bot_admin"])

    def test_get_helpers_fallback(self) -> None:
        """老 stub 没标权限属性 → helpers 返回 GUEST / None / False。"""
        tool = _StubTool("plain")
        self.assertEqual(
            get_tool_required_permission(tool), PermissionTier.GUEST
        )
        self.assertIsNone(get_tool_required_bot_role(tool))
        self.assertFalse(get_tool_require_bot_admin(tool))

    def test_get_required_bot_role_helper(self) -> None:
        """主契约 helper `get_tool_required_bot_role`：显式 "admin"/"owner" 原样返回；
        缺失回退旧 `require_bot_admin`（True→"admin"）；都没有 → None。"""

        class _Admin:
            required_bot_role = "admin"

        class _Owner:
            required_bot_role = "owner"

        class _LegacyBool:
            require_bot_admin = True

        class _None:
            pass

        self.assertEqual(get_tool_required_bot_role(_Admin()), "admin")
        self.assertEqual(get_tool_required_bot_role(_Owner()), "owner")
        self.assertEqual(get_tool_required_bot_role(_LegacyBool()), "admin")
        self.assertIsNone(get_tool_required_bot_role(_None()))

    def test_get_helpers_accept_str_and_int(self) -> None:
        """允许 tool 用字符串或 int 标记 required_permission（容错）。"""

        class _S:
            required_permission = "admin"

        class _I:
            required_permission = 30  # OWNER

        class _Garbage:
            required_permission = "not-a-tier"

        self.assertEqual(get_tool_required_permission(_S()), PermissionTier.ADMIN)
        self.assertEqual(get_tool_required_permission(_I()), PermissionTier.OWNER)
        self.assertEqual(
            get_tool_required_permission(_Garbage()), PermissionTier.GUEST
        )

    def test_len_and_contains(self) -> None:
        reg = ToolRegistry()
        self.assertEqual(len(reg), 0)
        self.assertNotIn("x", reg)
        reg.register(_StubTool("x"))
        self.assertEqual(len(reg), 1)
        self.assertIn("x", reg)
        self.assertNotIn(123, reg)  # type: ignore[operator]


class _SystemOnlyTool:
    name = "respond_to_request"
    description = "system only"
    arguments_schema = {"type": "object"}
    allowed_scopes = ("system",)
    usage_prompt = "SYSTEM TOOL USAGE BODY"

    async def run(self, arguments: dict, **_: Any) -> Any:
        return {}


class _PlainUsageTool:
    name = "send_message"
    description = "d"
    arguments_schema = {"type": "object"}
    usage_prompt = "SEND_MESSAGE USAGE BODY"

    async def run(self, arguments: dict, **_: Any) -> Any:
        return {}


class ToolScopeVisibilityTest(unittest.TestCase):
    """allowed_scopes per-scope 可见性（契约 §2.2：respond_to_request 仅
    SystemAgentLoop 可见，GroupAgentLoop 不知道它存在）。"""

    def _reg(self) -> ToolRegistry:
        reg = ToolRegistry()
        reg.register(_StubTool("send_message"))  # 不限 scope（allowed_scopes 缺失）
        reg.register(_SystemOnlyTool())
        return reg

    def test_catalog_no_scope_includes_all(self) -> None:
        # scope=None（旧调用）→ 不过滤，全部可见
        names = {e["name"] for e in self._reg().catalog()}
        self.assertEqual(names, {"send_message", "respond_to_request"})

    def test_catalog_system_scope_includes_scoped_tool(self) -> None:
        names = {e["name"] for e in self._reg().catalog("system")}
        self.assertIn("respond_to_request", names)
        self.assertIn("send_message", names)

    def test_catalog_group_scope_hides_scoped_tool(self) -> None:
        names = {e["name"] for e in self._reg().catalog("group")}
        self.assertIn("send_message", names)
        self.assertNotIn("respond_to_request", names)

    def test_usage_docs_group_scope_hides_scoped_tool(self) -> None:
        reg = ToolRegistry()
        reg.register(_PlainUsageTool())
        reg.register(_SystemOnlyTool())
        group_docs = reg.usage_docs("group")
        self.assertIn("SEND_MESSAGE USAGE BODY", group_docs)
        self.assertNotIn("SYSTEM TOOL USAGE BODY", group_docs)
        system_docs = reg.usage_docs("system")
        self.assertIn("SYSTEM TOOL USAGE BODY", system_docs)
        # 不传 scope → 不过滤，两段都在
        self.assertIn("SYSTEM TOOL USAGE BODY", reg.usage_docs())

    def test_get_tool_allowed_scopes_fallback(self) -> None:
        # 缺失 → None（不限）
        self.assertIsNone(get_tool_allowed_scopes(_StubTool("x")))
        self.assertEqual(
            get_tool_allowed_scopes(_SystemOnlyTool()), ("system",)
        )

        class _StrScope:
            allowed_scopes = "system"  # 字符串单值自动包成 tuple

        self.assertEqual(get_tool_allowed_scopes(_StrScope()), ("system",))


if __name__ == "__main__":
    unittest.main()

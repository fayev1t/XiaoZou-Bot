"""Contract tests for ToolRegistry.

Covers (任务与决策契约 §5.1):
- register / get / names / catalog 基本语义
- 重复 register 同名 → ValueError
- 缺 name 的 tool → ValueError
- catalog 渲染为 LLM 可消费的 [{name, description, arguments_schema,
  required_permission, require_bot_admin}] 列表
- 缺 required_permission / require_bot_admin 的老 stub 默认 GUEST / False
- 显式标注的 tool 正确透传
- in 运算符 / len 实现
"""

from __future__ import annotations

import unittest
from typing import Any

from qqbot.core.permissions import PermissionTier
from qqbot.services.agent_loop.tool_registry import (
    ToolRegistry,
    get_tool_require_bot_admin,
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
        # 老 stub 没标 required_permission / require_bot_admin → 默认 GUEST / False
        self.assertEqual(entry["required_permission"], "GUEST")
        self.assertFalse(entry["require_bot_admin"])

    def test_catalog_propagates_permission_metadata(self) -> None:
        class _AdminTool:
            name = "kick_member"
            description = "Kick a member"
            arguments_schema = {"type": "object"}
            required_permission = PermissionTier.ADMIN
            require_bot_admin = True

            async def run(self, arguments: dict, **_: Any) -> Any:
                return {}

        reg = ToolRegistry()
        reg.register(_AdminTool())
        entry = reg.catalog()[0]
        self.assertEqual(entry["required_permission"], "ADMIN")
        self.assertTrue(entry["require_bot_admin"])

    def test_get_helpers_fallback(self) -> None:
        """老 stub 没标这两个属性 → helpers 返回 GUEST / False。"""
        tool = _StubTool("plain")
        self.assertEqual(
            get_tool_required_permission(tool), PermissionTier.GUEST
        )
        self.assertFalse(get_tool_require_bot_admin(tool))

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


if __name__ == "__main__":
    unittest.main()

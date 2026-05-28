"""Contract tests for ToolRegistry.

Covers (任务与决策契约 §5.1):
- register / get / names / catalog 基本语义
- 重复 register 同名 → ValueError
- 缺 name 的 tool → ValueError
- catalog 渲染为 LLM 可消费的 [{name, description, arguments_schema}] 列表
- in 运算符 / len 实现
"""

from __future__ import annotations

import unittest
from typing import Any

from qqbot.services.agent_loop.tool_registry import ToolRegistry


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

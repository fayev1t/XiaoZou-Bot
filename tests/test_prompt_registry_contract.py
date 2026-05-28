"""PromptRegistry 自身的契约测试 —— 与 LLMPlanner 解耦，验证 section
注册 / order / 懒求值 / 异常吞掉。"""

from __future__ import annotations

import unittest

from qqbot.services.agent_loop.prompt_registry import PromptRegistry


class PromptRegistryOrderingTests(unittest.TestCase):
    def test_render_concatenates_in_order(self) -> None:
        reg = PromptRegistry()
        reg.register("b", 10, "BODY-B")
        reg.register("a", 0, "BODY-A")
        reg.register("c", 20, "BODY-C")

        out = reg.render()
        # 按 order 升序，分隔符 \n\n---\n\n
        self.assertEqual(out, "BODY-A\n\n---\n\nBODY-B\n\n---\n\nBODY-C")
        self.assertEqual(reg.section_names(), ["a", "b", "c"])

    def test_callable_source_resolved_at_render_time(self) -> None:
        reg = PromptRegistry()
        state = {"val": "first"}
        reg.register("dyn", 0, lambda: state["val"])

        self.assertEqual(reg.render(), "first")
        state["val"] = "second"
        # 后续 render 必须看到最新值（懒求值，不缓存）
        self.assertEqual(reg.render(), "second")

    def test_empty_sections_dropped_no_orphan_separator(self) -> None:
        reg = PromptRegistry()
        reg.register("head", 0, "HEAD")
        reg.register("blank-str", 5, "   \n  ")  # whitespace-only
        reg.register("blank-cb", 6, lambda: "")
        reg.register("tail", 10, "TAIL")

        out = reg.render()
        self.assertEqual(out, "HEAD\n\n---\n\nTAIL")
        # 空段不应产生 `---\n\n---` 之类的孤儿分隔符
        self.assertNotIn("---\n\n---", out)

    def test_register_same_name_overwrites(self) -> None:
        reg = PromptRegistry()
        reg.register("x", 0, "OLD")
        reg.register("x", 0, "NEW")
        self.assertEqual(reg.render(), "NEW")

    def test_callable_exception_silently_dropped(self) -> None:
        reg = PromptRegistry()
        reg.register("ok", 0, "OK")

        def boom() -> str:
            raise RuntimeError("intentional")

        reg.register("boom", 5, boom)
        reg.register("tail", 10, "TAIL")

        # boom 段被静默丢弃，不应阻塞整段 render
        out = reg.render()
        self.assertEqual(out, "OK\n\n---\n\nTAIL")

    def test_remove_section(self) -> None:
        reg = PromptRegistry()
        reg.register("a", 0, "A")
        reg.register("b", 10, "B")
        reg.remove("a")
        self.assertEqual(reg.render(), "B")
        self.assertFalse(reg.has("a"))
        self.assertTrue(reg.has("b"))

    def test_register_requires_name(self) -> None:
        reg = PromptRegistry()
        with self.assertRaises(ValueError):
            reg.register("", 0, "X")


if __name__ == "__main__":
    unittest.main()

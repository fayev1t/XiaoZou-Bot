"""PromptRegistry 自身的契约测试 —— 与 LLMPlanner 解耦，验证 section
注册 / order / 懒求值 / 异常吞掉。"""

from __future__ import annotations

import unittest

from qqbot.services.agent_loop.prompt_registry import (
    SECTION_SEP,
    PromptRegistry,
)


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

    def test_render_passes_scope_to_scope_aware_source(self) -> None:
        # render(scope=...) 把 scope 传给"接受一个位置参"的 source（如
        # ToolRegistry.usage_docs），字符串 / 无参 source 照常不受影响。这是
        # tools_usage 按 scope 过滤、避免群↔system 工具用法互相泄漏的落地机制。
        seen: list = []

        def scope_aware(scope=None):
            seen.append(scope)
            return f"USAGE[{scope}]"

        reg = PromptRegistry()
        reg.register("persona", 0, "PERSONA")  # str
        reg.register("noarg", 5, lambda: "NOARG")  # () -> str
        reg.register("usage", 10, scope_aware)  # (scope) -> str

        out = reg.render(scope="group")
        self.assertIn("PERSONA", out)
        self.assertIn("NOARG", out)
        self.assertIn("USAGE[group]", out)
        self.assertEqual(seen, ["group"])  # scope 确实传进去了

    def test_render_sections_matches_render(self) -> None:
        # render_sections 是 render 的分段视图（Prompt 快照统计每段体积用）：
        # 同序、同过滤、join(SECTION_SEP) 后与 render() 逐字节一致。
        reg = PromptRegistry()
        reg.register("b", 10, "BODY-B")
        reg.register("a", 0, "BODY-A")
        reg.register("blank", 5, "   ")  # 空段照旧丢弃

        def boom() -> str:
            raise RuntimeError("intentional")

        reg.register("boom", 7, boom)  # 异常段照旧丢弃

        sections = reg.render_sections()
        self.assertEqual([s.name for s in sections], ["a", "b"])
        self.assertEqual([s.text for s in sections], ["BODY-A", "BODY-B"])
        self.assertEqual(
            SECTION_SEP.join(s.text for s in sections), reg.render()
        )

    def test_render_sections_passes_scope(self) -> None:
        reg = PromptRegistry()
        reg.register("usage", 0, lambda scope=None: f"U[{scope}]")
        sections = reg.render_sections(scope="group")
        self.assertEqual(sections[0].text, "U[group]")

    def test_render_default_scope_none_backward_compatible(self) -> None:
        # 不传 scope（旧调用 / 单测）→ scope-aware source 收到 None（= 不过滤）；
        # 无参 source 一如既往。
        seen: list = []

        def scope_aware(scope=None):
            seen.append(scope)
            return f"U[{scope}]"

        reg = PromptRegistry()
        reg.register("noarg", 0, lambda: "NOARG")
        reg.register("usage", 10, scope_aware)
        out = reg.render()  # 无 scope 参
        self.assertIn("NOARG", out)
        self.assertIn("U[None]", out)
        self.assertEqual(seen, [None])


if __name__ == "__main__":
    unittest.main()

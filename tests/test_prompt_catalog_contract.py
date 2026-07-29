"""prompts/catalog.py 收口层契约（2026-07-27）。

钉住四件事：
1. 装配单本身（哪个消费者拿哪些段、什么顺序）——收口后这就是三个 LLM 的
   prompt 组成的唯一权威，改动必须是有意的；
2. 分层红线是**结构性**的：persona（voice）进 Planner、policy/protocol/
   tools 进 Replyer，build 期直接炸，不再只靠注释与自觉；
3. required 失败语义（待办#17 目标 2 前半）：关键段缺失/为空 fail loudly，
   非 required 段维持降级；source 返回 None = 本 scope 不适用，主动跳过；
4. 装配产物与 prompts/*.md 文件逐字节对账（外置重构不改内容的护栏）。

文件读取走真实 prompts/ 目录（与部署同源）；registry 内核语义用内联 fake
source，不依赖文件系统。
"""

from __future__ import annotations

import unittest

from qqbot.services.agent_loop.prompt_registry import (
    SECTION_SEP,
    PromptRegistry,
    PromptSectionMissing,
)
from qqbot.services.agent_loop.prompts.catalog import (
    _PROMPTS_DIR,
    ASSEMBLY,
    SECTIONS,
    PromptAssemblyError,
    SectionSpec,
    _file_source,
    _validate_assembly,
    build_registry,
    render_system_prompt,
)


class AssemblyPinningTests(unittest.TestCase):
    def test_assembly_lists_are_pinned(self) -> None:
        self.assertEqual(
            ASSEMBLY,
            {
                "planner": (
                    "identity",
                    "xml_format",
                    "group_chat_rules",
                    "protocol",
                    "tools_usage",
                ),
                "replyer": ("replyer_composer", "voice"),
                "caption": ("meme_caption",),
                "image_description": ("image_description",),
                "image_look": ("image_look",),
                "memory": ("memory_compaction",),
            },
        )

    def test_red_line_kinds_are_pinned(self) -> None:
        """红线靠 kind 生效；改掉 kind 等于拆红线，必须显式过这里。"""
        self.assertEqual(SECTIONS["voice"].kind, "persona")
        self.assertEqual(SECTIONS["group_chat_rules"].kind, "policy")
        self.assertEqual(SECTIONS["protocol"].kind, "protocol")
        self.assertEqual(SECTIONS["tools_usage"].kind, "tools")

    def test_real_assemblies_pass_validation(self) -> None:
        for consumer, names in ASSEMBLY.items():
            _validate_assembly(consumer, names)

    def test_only_tools_usage_is_optional(self) -> None:
        """tools_usage 之外全部 required——关键段静默丢失正是 #17 要堵的。"""
        for name, spec in SECTIONS.items():
            self.assertEqual(spec.required, name != "tools_usage", f"section {name}")


class RedLineTests(unittest.TestCase):
    def test_persona_forbidden_in_planner(self) -> None:
        with self.assertRaisesRegex(PromptAssemblyError, "voice"):
            _validate_assembly("planner", ("identity", "voice"))

    def test_policy_forbidden_in_replyer(self) -> None:
        with self.assertRaisesRegex(PromptAssemblyError, "group_chat_rules"):
            _validate_assembly("replyer", ("replyer_composer", "group_chat_rules"))

    def test_protocol_forbidden_in_replyer(self) -> None:
        with self.assertRaisesRegex(PromptAssemblyError, "protocol"):
            _validate_assembly("replyer", ("protocol",))

    def test_tools_forbidden_in_replyer(self) -> None:
        with self.assertRaisesRegex(PromptAssemblyError, "tools_usage"):
            _validate_assembly("replyer", ("tools_usage",))

    def test_persona_forbidden_in_memory(self) -> None:
        """记忆压缩是事实记录员：人格进不来（记忆系统契约 §5.1）。"""
        with self.assertRaisesRegex(PromptAssemblyError, "voice"):
            _validate_assembly("memory", ("memory_compaction", "voice"))

    def test_unknown_section_rejected(self) -> None:
        with self.assertRaisesRegex(PromptAssemblyError, "unknown section"):
            _validate_assembly("planner", ("identity", "no_such_section"))

    def test_unknown_consumer_rejected(self) -> None:
        with self.assertRaisesRegex(PromptAssemblyError, "unknown prompt consumer"):
            _validate_assembly("judge", ())


class RequiredSemanticsTests(unittest.TestCase):
    """registry 内核的 required 分流（与 catalog 解耦，纯内联 source）。"""

    def test_required_source_exception_propagates(self) -> None:
        reg = PromptRegistry()

        def boom() -> str:
            raise ValueError("deployment broken")

        reg.register("key", 0, boom, required=True)
        with self.assertRaisesRegex(ValueError, "deployment broken"):
            reg.render()

    def test_required_empty_raises_missing(self) -> None:
        reg = PromptRegistry()
        reg.register("key", 0, "   ", required=True)
        with self.assertRaisesRegex(PromptSectionMissing, "key"):
            reg.render()

    def test_optional_failure_still_degrades(self) -> None:
        reg = PromptRegistry()

        def boom() -> str:
            raise ValueError("one tool doc missing")

        reg.register("optional", 0, boom)
        reg.register("kept", 10, "body", required=True)
        self.assertEqual(reg.render(), "body")

    def test_none_skip_bypasses_required(self) -> None:
        """None = 本 scope 不适用（条件装配通道），required 段照样跳过。"""
        reg = PromptRegistry()
        reg.register("cond", 0, lambda scope=None: None, required=True)
        reg.register("kept", 10, "body", required=True)
        self.assertEqual(reg.render(scope="system"), "body")

    def test_missing_required_file_fails_loudly(self) -> None:
        spec = SectionSpec(
            name="ghost", order=0, kind="doc", filename="__no_such_prompt__.md"
        )
        reg = PromptRegistry()
        reg.register("ghost", 0, _file_source(spec), required=True)
        with self.assertRaises(OSError):
            reg.render()


class FileAssemblyTests(unittest.TestCase):
    """装配产物 ↔ prompts/*.md 逐字节对账。"""

    @staticmethod
    def _md(name: str) -> str:
        return (_PROMPTS_DIR / name).read_text(encoding="utf-8").strip()

    def test_planner_render_matches_md_files(self) -> None:
        rendered = build_registry("planner").render(scope="group")
        expected = SECTION_SEP.join(
            self._md(name)
            for name in (
                "identity.md",
                "xml_format.md",
                "group_chat_rules.md",
                "protocol.md",
            )
        )
        self.assertEqual(rendered, expected)

    def test_system_scope_drops_group_chat_rules(self) -> None:
        names = [
            sec.name
            for sec in build_registry("planner").render_sections(scope="system")
        ]
        self.assertEqual(names, ["identity", "xml_format", "protocol"])

    def test_legacy_planner_entry_delegates_to_catalog(self) -> None:
        from qqbot.services.agent_loop.llm_planner import (
            build_default_prompt_registry,
        )

        self.assertEqual(
            build_default_prompt_registry().render(scope="group"),
            build_registry("planner").render(scope="group"),
        )

    def test_tools_usage_rendered_with_registry(self) -> None:
        from qqbot.services.agent_loop.tools import build_default_registry

        sections = build_registry(
            "planner", tool_registry=build_default_registry()
        ).render_sections(scope="group")
        names = [sec.name for sec in sections]
        self.assertEqual(names[-1], "tools_usage")
        self.assertTrue(sections[-1].text)

    def test_replyer_render_is_composer_plus_voice(self) -> None:
        rendered = render_system_prompt("replyer")
        self.assertEqual(
            rendered,
            self._md("replyer.md") + SECTION_SEP + self._md("voice.md"),
        )
        # 组稿指令与角色卡都真实在场（锚点与 test_replyer_contract 互补）。
        self.assertIn("final visible-reply composer", rendered)
        self.assertIn("小奏", rendered)

    def test_caption_render_matches_file(self) -> None:
        rendered = render_system_prompt("caption")
        self.assertEqual(rendered, self._md("meme_caption.md"))
        # 限长锚点：2026-07-27 由 120 字放宽到 150（给"适用场景"留篇幅），
        # 当时漏改这条断言。数字本身不是契约，"有硬限长"才是。
        self.assertIn("150 字", rendered)

    def test_image_description_render_matches_file(self) -> None:
        rendered = render_system_prompt("image_description")
        self.assertEqual(rendered, self._md("image_description.md"))

    def test_memory_render_matches_file(self) -> None:
        rendered = render_system_prompt("memory")
        self.assertEqual(rendered, self._md("memory_compaction.md"))
        self.assertIn("<recall-cues>", rendered)
        self.assertNotIn("只输出一个 JSON 对象", rendered)


if __name__ == "__main__":
    unittest.main()

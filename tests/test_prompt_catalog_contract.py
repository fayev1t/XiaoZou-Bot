"""prompts/catalog.py 提示词库契约。

钉住四件事：
1. 装配单本身（哪个消费者取哪几段、什么顺序）——它是六个 LLM 调用点的 prompt
   组成的唯一权威，改动必须是有意的；
2. 分工的**语义**边界：角色卡正文不得出现在 Planner 的渲染结果里，决策 JSON
   契约与参与政策不得出现在 Replyer 的渲染结果里。2026-07-30 删掉
   kind/_FORBIDDEN_KINDS/_validate_assembly 之后，这类断言是唯一还抓得到内容
   漂移的手段——结构校验的粒度是"哪个文件进哪个消费者"，而真实发生过的事故是
   "人格正文被抄进另一个文件"，那种它一声不响；
3. 空文件 fail loudly（部署坏了不静默跑残缺 prompt），动态段求值为空则跳过；
4. 装配产物与 prompts/*.md 文件逐字节对账（重构不改内容的护栏）。

文件读取走真实 prompts/ 目录（与部署同源）；库内核语义用内联 fake source，
不依赖文件系统。
"""

from __future__ import annotations

import unittest

from qqbot.services.agent_loop.prompts.catalog import (
    _FILES,
    _PROMPTS_DIR,
    ASSEMBLY,
    SECTION_SEP,
    PromptLibrary,
    PromptSectionMissing,
    build_library,
    render_system_prompt,
)


class AssemblyPinningTests(unittest.TestCase):
    def test_assembly_lists_are_pinned(self) -> None:
        self.assertEqual(
            ASSEMBLY,
            {
                "planner": (
                    "planner",
                    "envelope",
                    "group_chat_rules",
                    "tools_usage",
                ),
                "replyer": ("replyer", "envelope"),
                "caption": ("meme_caption",),
                "image_description": ("image_description",),
                "image_look": ("image_look",),
                "memory": ("memory_compaction",),
            },
        )

    def test_every_file_section_has_a_real_file(self) -> None:
        for name, filename in _FILES.items():
            with self.subTest(section=name):
                self.assertTrue((_PROMPTS_DIR / filename).is_file(), filename)

    def test_assembly_only_references_known_sections(self) -> None:
        """动态段只剩 tools_usage；其余必须在 _FILES 里登记。"""
        dynamic = {"tools_usage"}
        for consumer, names in ASSEMBLY.items():
            for name in names:
                with self.subTest(consumer=consumer, section=name):
                    self.assertIn(name, set(_FILES) | dynamic)

    def test_unknown_consumer_raises(self) -> None:
        """未登记的消费者必须炸，不能静默给出空 system prompt。"""
        with self.assertRaises(KeyError):
            build_library("no_such_consumer")

    def test_legacy_assets_are_absent(self) -> None:
        """历史资产不得复活——两份都在时改一处忘另一处就是两个真相源。"""
        for stale in (
            "xml_format.md",
            "protocol.md",
            "identity.md",
            "disposition.md",
        ):
            self.assertFalse((_PROMPTS_DIR / stale).exists(), stale)
        for stale in ("xml_format", "protocol", "identity", "replyer_composer"):
            self.assertNotIn(stale, _FILES)


class LayerBoundaryTests(unittest.TestCase):
    """分工的语义边界 —— kind 结构校验删除之后的唯一防线。"""

    @staticmethod
    def _long_lines(filename: str, *, prefix: str | None = None) -> list[str]:
        text = (_PROMPTS_DIR / filename).read_text(encoding="utf-8")
        return [
            line.strip()
            for line in text.splitlines()
            if len(line.strip()) > 24
            and (prefix is None or line.strip().startswith(prefix))
        ]

    def test_persona_body_never_reaches_planner(self) -> None:
        """角色卡第二人称正文漏进决策层，reasoning 就会开始用她的语气演戏、
        拿心情当调工具的理由。角色卡 2026-07-30 起住在 replyer.md 里。"""
        rendered = build_library("planner").render(scope="group")
        lines = self._long_lines("replyer.md", prefix="你")
        self.assertTrue(lines, "replyer.md 没有第二人称锚点，断言会假通过")
        for line in lines:
            self.assertNotIn(line, rendered)

    def test_decision_output_contract_never_reaches_replyer(self) -> None:
        """一个 prompt 两套输出 JSON 规范必然串台。"""
        rendered = build_library("replyer").render()
        self.assertNotIn('"type":"call_tool"', rendered)
        self.assertNotIn('"actions"', rendered)

    def test_participation_policy_never_reaches_replyer(self) -> None:
        """组稿层手上的稿子已获授权；重读参与政策会二次审查并产出空回复，
        架空上一步的决定。"""
        rendered = build_library("replyer").render()
        lines = self._long_lines("group_chat_rules.md")
        self.assertTrue(lines, "group_chat_rules.md 没有可用锚点")
        for line in lines:
            self.assertNotIn(line, rendered)

    def test_record_layers_read_only_their_own_section(self) -> None:
        """纯记录/观察层的输出会被永久写进事件正文并被下游反复读取，掺进人格
        或群规就是污染所有下游语境（记忆系统契约 §5.1）。"""
        for consumer in (
            "caption",
            "image_description",
            "image_look",
            "memory",
        ):
            with self.subTest(consumer=consumer):
                self.assertEqual(len(ASSEMBLY[consumer]), 1)


class LibraryKernelTests(unittest.TestCase):
    """持有 + 按名字取的内核语义（纯内联 source，不碰文件系统）。"""

    def test_sections_render_in_listed_order(self) -> None:
        lib = PromptLibrary([("a", "AAA"), ("b", "BBB")])
        self.assertEqual(lib.section_names(), ["a", "b"])
        self.assertEqual(lib.render(), f"AAA{SECTION_SEP}BBB")

    def test_get_by_name(self) -> None:
        lib = PromptLibrary([("a", "AAA"), ("b", lambda: "BBB")])
        self.assertEqual(lib.get("a"), "AAA")
        self.assertEqual(lib.get("b"), "BBB")
        with self.assertRaises(KeyError):
            lib.get("nope")

    def test_add_overwrites_in_place(self) -> None:
        lib = PromptLibrary([("a", "AAA"), ("b", "BBB")])
        lib.add("a", "NEW")
        self.assertEqual(lib.section_names(), ["a", "b"])
        self.assertEqual(lib.get("a"), "NEW")

    def test_remove_and_has(self) -> None:
        lib = PromptLibrary([("a", "AAA")])
        self.assertTrue(lib.has("a"))
        lib.remove("a")
        self.assertFalse(lib.has("a"))
        self.assertEqual(lib.render(), "")

    def test_callable_source_is_lazy(self) -> None:
        """render 时才求值 —— 改 .md 立即生效靠的就是这一点。"""
        calls: list[int] = []

        def source() -> str:
            calls.append(1)
            return "X"

        lib = PromptLibrary([("a", source)])
        self.assertEqual(calls, [])
        lib.render()
        self.assertEqual(calls, [1])

    def test_scope_is_passed_only_to_sources_that_accept_it(self) -> None:
        lib = PromptLibrary(
            [("scoped", lambda scope: f"S={scope}"), ("plain", lambda: "P")]
        )
        self.assertEqual(lib.render(scope="group"), f"S=group{SECTION_SEP}P")

    def test_empty_dynamic_section_is_skipped(self) -> None:
        lib = PromptLibrary([("a", "AAA"), ("tools_usage", lambda: "  ")])
        self.assertEqual(lib.render(), "AAA")

    def test_empty_file_section_fails_loudly(self) -> None:
        """文件段为空 = 部署坏了，绝不静默跑残缺 prompt。"""
        lib = PromptLibrary([("planner", lambda: "")])
        with self.assertRaisesRegex(PromptSectionMissing, "planner"):
            lib.render()

    def test_source_exception_propagates(self) -> None:
        def boom() -> str:
            raise ValueError("deployment broken")

        lib = PromptLibrary([("planner", boom)])
        with self.assertRaisesRegex(ValueError, "deployment broken"):
            lib.render()

    def test_render_sections_matches_render(self) -> None:
        lib = PromptLibrary([("a", "AAA"), ("b", "BBB")])
        sections = lib.render_sections()
        self.assertEqual([s.name for s in sections], ["a", "b"])
        self.assertEqual(
            SECTION_SEP.join(s.text for s in sections), lib.render()
        )


class FileAssemblyTests(unittest.TestCase):
    """装配产物 ↔ prompts/*.md 逐字节对账。"""

    @staticmethod
    def _md(name: str) -> str:
        return (_PROMPTS_DIR / name).read_text(encoding="utf-8").strip()

    def test_planner_render_matches_md_files(self) -> None:
        rendered = build_library("planner").render(scope="group")
        expected = SECTION_SEP.join(
            self._md(name)
            for name in ("planner.md", "envelope.md", "group_chat_rules.md")
        )
        self.assertEqual(rendered, expected)

    def test_envelope_is_shared_byte_for_byte(self) -> None:
        """两个消费者读到的信封语法必须是同一份字节：Replyer 的
        <replyer-input> 里 timeline 由同一个 render_timeline_stream 渲染，
        任何一侧另写一份缩写说明都会漂。"""
        planner_env = build_library("planner").get("envelope")
        replyer_env = build_library("replyer").get("envelope")
        self.assertEqual(planner_env, replyer_env)
        self.assertEqual(planner_env, self._md("envelope.md"))
        for tag in ("<agent-input>", "<replyer-input>", "<reply-task>"):
            self.assertIn(tag, planner_env)

    def test_planner_entry_delegates_to_catalog(self) -> None:
        from qqbot.services.agent_loop.llm_planner import (
            build_default_prompt_library,
        )

        self.assertEqual(
            build_default_prompt_library().render(scope="group"),
            build_library("planner").render(scope="group"),
        )

    def test_tools_usage_rendered_with_registry(self) -> None:
        from qqbot.services.agent_loop.tools import build_default_registry

        sections = build_library(
            "planner", tool_registry=build_default_registry()
        ).render_sections(scope="group")
        names = [sec.name for sec in sections]
        self.assertEqual(names[-1], "tools_usage")
        self.assertTrue(sections[-1].text)

    def test_tools_usage_skipped_without_registry(self) -> None:
        names = [
            sec.name for sec in build_library("planner").render_sections()
        ]
        self.assertNotIn("tools_usage", names)

    def test_replyer_render_is_job_page_plus_envelope(self) -> None:
        rendered = render_system_prompt("replyer")
        self.assertEqual(
            rendered,
            self._md("replyer.md") + SECTION_SEP + self._md("envelope.md"),
        )
        # 组稿职责与角色卡同在一份 replyer.md 里（voice.md 2026-07-30 并入后删除）
        self.assertIn("小奏", rendered)
        self.assertIn("<reply-task>", rendered)

    def test_legacy_voice_asset_is_absent(self) -> None:
        """角色卡并入 replyer.md 后 voice.md 不得复活：两份都在时 Replyer 的
        prompt 里会前后各读一遍人格，改一处就当场自相矛盾。"""
        self.assertFalse((_PROMPTS_DIR / "voice.md").exists())
        self.assertNotIn("voice", _FILES)
        for names in ASSEMBLY.values():
            self.assertNotIn("voice", names)

    def test_caption_render_matches_file(self) -> None:
        rendered = render_system_prompt("caption")
        self.assertEqual(rendered, self._md("meme_caption.md"))
        # 限长锚点：2026-07-27 由 120 字放宽到 150（给"适用场景"留篇幅），
        # 当时漏改这条断言。数字本身不是契约，"有硬限长"才是。
        self.assertIn("150 字", rendered)

    def test_image_description_render_matches_file(self) -> None:
        rendered = render_system_prompt("image_description")
        self.assertEqual(rendered, self._md("image_description.md"))

    def test_image_look_render_matches_file(self) -> None:
        rendered = render_system_prompt("image_look")
        self.assertEqual(rendered, self._md("image_look.md"))

    def test_memory_render_matches_file(self) -> None:
        rendered = render_system_prompt("memory")
        self.assertEqual(rendered, self._md("memory_compaction.md"))
        self.assertIn("<recall-cues>", rendered)
        self.assertNotIn("只输出一个 JSON 对象", rendered)


if __name__ == "__main__":
    unittest.main()

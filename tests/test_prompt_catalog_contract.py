"""prompts/catalog.py 提示词库契约。

装配机制 2026-07-30 统一为**根页 + `{{槽}}`**：`CONSUMERS` 把消费者映射到根页
`.md`，页正文里的 `{{name}}` 决定要哪几段、什么顺序、怎么分隔。`ASSEMBLY` 与
`SECTION_SEP` 已删除，所以本文件不再钉"段清单"，改为钉每张根页实际用到的槽序列。

钉住四件事：
1. 根页登记与各页的槽序列——它们是五个 LLM 调用点的 prompt 组成的唯一权威，
   改动必须是有意的（2026-07-31 删除 Replyer 后 Planner 是唯一对话消费者）；
2. 分工的**语义**边界：角色卡正文**必须**出现在 Planner 的渲染结果里且以
   第二人称直接在页首（注入断了要当场红，而不是安静地少一段人格）；纯记录/
   观察层的根页无槽。2026-07-30 删掉 kind/_FORBIDDEN_KINDS/_validate_assembly
   之后，这类断言是唯一还抓得到内容漂移的手段——结构校验的粒度是"哪个文件进
   哪个消费者"，而真实发生过的事故是"人格正文被抄进另一个文件"，那种它一声
   不响；
3. 空文件 fail loudly（部署坏了不静默跑残缺 prompt），动态段求值为空则跳过；
4. 装配产物与 prompts/*.md 文件逐字节对账（重构不改内容的护栏）。

文件读取走真实 prompts/ 目录（与部署同源）；库内核语义用内联 fake source，
不依赖文件系统。
"""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from qqbot.services.agent_loop.prompts.catalog import (
    _FILES,
    _PROMPTS_DIR,
    CONSUMERS,
    DYNAMIC_SLOTS,
    SLOT_PATTERN,
    PromptLibrary,
    PromptSectionMissing,
    build_library,
    render_system_prompt,
)


class AssemblyPinningTests(unittest.TestCase):
    """装配现在完全写在根页的 `{{槽}}` 里（2026-07-30 起 ASSEMBLY 已删除）。"""

    def test_consumers_and_root_pages_are_pinned(self) -> None:
        self.assertEqual(
            CONSUMERS,
            {
                "planner": "planner.md",
                "caption": "meme_caption.md",
                "image_description": "image_description.md",
                "image_look": "image_look.md",
                "memory": "memory_compaction.md",
            },
        )

    def test_page_slot_lists_are_pinned(self) -> None:
        """每张根页实际用到哪几个槽、什么顺序——改动必须是有意的。"""
        self.assertEqual(
            build_library("planner").slot_names(),
            ["persona", "system", "envelope", "group_chat_rules", "tools_usage"],
        )

    def test_every_file_slot_has_a_real_file(self) -> None:
        for name, filename in {**_FILES, **CONSUMERS}.items():
            with self.subTest(asset=name):
                self.assertTrue((_PROMPTS_DIR / filename).is_file(), filename)

    def test_root_pages_are_not_slots(self) -> None:
        """硬规则：根页之间永不互相开槽。根页不登记为槽，页里写别的消费者名
        会按未知槽名炸掉。"""
        for consumer in CONSUMERS:
            with self.subTest(consumer=consumer):
                self.assertNotIn(consumer, _FILES)
        slots = build_library("planner").slot_names()
        for other in CONSUMERS:
            with self.subTest(other=other):
                self.assertNotIn(other, slots)

    def test_pages_only_reference_known_slots(self) -> None:
        """页里写的每个槽都必须能解析：文件槽在 _FILES，动态槽在 DYNAMIC_SLOTS。
        写错一个名字就是让 `{{typo}}` 字面量出现在模型眼前。"""
        known = set(_FILES) | set(DYNAMIC_SLOTS)
        for consumer, filename in CONSUMERS.items():
            text = (_PROMPTS_DIR / filename).read_text(encoding="utf-8")
            for match in SLOT_PATTERN.finditer(text):
                with self.subTest(consumer=consumer, slot=match.group(1)):
                    self.assertIn(match.group(1), known)

    def test_unknown_consumer_raises(self) -> None:
        """未登记的消费者必须炸，不能静默给出空 system prompt。"""
        with self.assertRaises(KeyError):
            build_library("no_such_consumer")

    def test_legacy_assets_are_absent(self) -> None:
        """历史资产不得复活——两份都在时改一处忘另一处就是两个真相源。
        replyer.md 随 2026-07-31 删除 Replyer 一并删除。"""
        for stale in (
            "xml_format.md",
            "protocol.md",
            "identity.md",
            "disposition.md",
            "replyer.md",
        ):
            self.assertFalse((_PROMPTS_DIR / stale).exists(), stale)
        for stale in (
            "xml_format",
            "protocol",
            "identity",
            "replyer",
            "replyer_composer",
        ):
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

    def test_persona_card_reaches_the_planner(self) -> None:
        """角色卡注入唯一的对话消费者 Planner（2026-07-31 删除 Replyer 后分析
        与措辞同归一层）。钉的是**注入没断**：槽位被编辑掉、卡片被改名或清空，
        都要在这里当场红。锚点从 persona.md 现取，卡片重写后断言跟着走、不会
        假通过。"""
        lines = self._long_lines("persona.md", prefix="你")
        self.assertTrue(lines, "persona.md 没有第二人称锚点，断言会假通过")
        rendered = build_library("planner").render(scope="group")
        for line in lines:
            self.assertIn(line, rendered)
        self.assertIsNone(SLOT_PATTERN.search(rendered))

    def test_planner_owns_the_card_first_person(self) -> None:
        """卡片在页首、以第二人称直接对 Planner 说话——那就是她自己。旧的
        「这个qq号背后的人格是…」第三方框定引导语随 Replyer 一并退役：它存在
        的唯一理由是把卡片框成对下游角色的描述，而下游角色已不存在。"""
        rendered = build_library("planner").render(scope="group")
        first = self._long_lines("persona.md", prefix="你")[0]
        self.assertNotIn("这个qq号背后的人格", rendered)
        # 卡片正文先于页面其余部分出现（页首即人格）。
        self.assertLess(
            rendered.index(first), rendered.index("# 你在怎样运行")
        )

    def test_real_assembly_fails_loudly_when_the_card_is_empty(self) -> None:
        """卡片为空必须 raise：残缺人格的 prompt 照跑，产出的是一个没有性格的
        账号，而从日志上看一切正常——这正是最难发现的坏法。走真实装配路径
        （build_library），不只是内核。"""
        from unittest.mock import patch

        from qqbot.services.agent_loop.prompts import catalog

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for filename in {**_FILES, **CONSUMERS}.values():
                (root / filename).write_text(
                    (_PROMPTS_DIR / filename).read_text(encoding="utf-8"),
                    encoding="utf-8",
                )
            (root / "persona.md").write_text("   \n", encoding="utf-8")
            with patch.object(catalog, "_PROMPTS_DIR", root):
                with self.assertRaisesRegex(
                    PromptSectionMissing, "persona"
                ):
                    build_library("planner").render(scope="group")

    def test_record_layers_read_only_their_own_page(self) -> None:
        """纯记录/观察层的输出会被永久写进事件正文并被下游反复读取，掺进人格
        或群规就是污染所有下游语境（记忆系统契约 §5.1）。它们的根页里不许有
        任何槽。"""
        for consumer in (
            "caption",
            "image_description",
            "image_look",
            "memory",
        ):
            with self.subTest(consumer=consumer):
                self.assertEqual(build_library(consumer).slot_names(), [])


class LibraryKernelTests(unittest.TestCase):
    """根页 + 槽的内核语义（纯内联 source，不碰文件系统）。"""

    def test_slots_expand_in_page_order(self) -> None:
        lib = PromptLibrary("A\n\n{{x}}\n\nB\n\n{{y}}", {"x": "XX", "y": "YY"})
        self.assertEqual(lib.slot_names(), ["x", "y"])
        self.assertEqual(lib.render(), "A\n\nXX\n\nB\n\nYY")

    def test_page_without_slots_is_verbatim(self) -> None:
        lib = PromptLibrary("just a page\n", {"x": "XX"})
        self.assertEqual(lib.slot_names(), [])
        self.assertEqual(lib.render(), "just a page")

    def test_separators_come_from_the_page_not_the_library(self) -> None:
        """分隔符是页正文里的字符——库不再替页决定段之间长什么样。"""
        lib = PromptLibrary("{{x}}\n---\n{{y}}", {"x": "XX", "y": "YY"})
        self.assertEqual(lib.render(), "XX\n---\nYY")

    def test_get_by_name(self) -> None:
        lib = PromptLibrary("{{a}}{{b}}", {"a": "AAA", "b": lambda: "BBB"})
        self.assertEqual(lib.get("a"), "AAA")
        self.assertEqual(lib.get("b"), "BBB")
        with self.assertRaises(KeyError):
            lib.get("nope")

    def test_add_overwrites_and_remove_has(self) -> None:
        lib = PromptLibrary("{{a}}", {"a": "AAA"})
        self.assertTrue(lib.has("a"))
        lib.add("a", "NEW")
        self.assertEqual(lib.render(), "NEW")
        lib.remove("a")
        self.assertFalse(lib.has("a"))

    def test_callable_source_is_lazy(self) -> None:
        """render 时才求值 —— 改 .md 立即生效靠的就是这一点。"""
        calls: list[int] = []

        def source() -> str:
            calls.append(1)
            return "X"

        lib = PromptLibrary("{{a}}", {"a": source})
        self.assertEqual(calls, [])
        lib.render()
        self.assertEqual(calls, [1])

    def test_scope_is_passed_only_to_sources_that_accept_it(self) -> None:
        """钉的是 scope 只路由给收位置参的 source。槽用换行分隔——同一行内
        槽左右的空格按设计会被替换吃掉（见模块 docstring），这里不顺带把
        空格行为钉成契约。"""
        lib = PromptLibrary(
            "{{scoped}}\n{{plain}}",
            {"scoped": lambda scope: f"S={scope}", "plain": lambda: "P"},
        )
        self.assertEqual(lib.render(scope="group"), "S=group\nP")

    def test_empty_dynamic_slot_takes_its_separator_with_it(self) -> None:
        """未注入工具注册表时 tools_usage 求值为空：整槽跳过，且不能在页尾
        留下一条孤零零的分隔线。"""
        lib = PromptLibrary("AAA\n\n---\n\n{{tools_usage}}\n", {"tools_usage": "  "})
        self.assertEqual(lib.render(), "AAA")

    def test_empty_file_slot_fails_loudly(self) -> None:
        """文件槽为空 = 部署坏了，绝不静默跑残缺 prompt。"""
        lib = PromptLibrary("{{persona}}", {"persona": lambda: ""})
        with self.assertRaisesRegex(PromptSectionMissing, "persona"):
            lib.render()

    def test_empty_page_fails_loudly(self) -> None:
        lib = PromptLibrary(lambda: "   ", {}, name="planner")
        with self.assertRaisesRegex(PromptSectionMissing, "planner"):
            lib.render()

    def test_unknown_slot_fails_loudly(self) -> None:
        """槽名写错/资产改名时静默留下一个 `{{typo}}` 字面量会直接出现在模型
        眼前，比缺一整段更难发现。"""
        lib = PromptLibrary("A {{nope}} B", {"a": "AAA"}, name="planner")
        with self.assertRaisesRegex(PromptSectionMissing, "nope"):
            lib.render()

    def test_source_exception_propagates(self) -> None:
        def boom() -> str:
            raise ValueError("deployment broken")

        lib = PromptLibrary("{{a}}", {"a": boom})
        with self.assertRaisesRegex(ValueError, "deployment broken"):
            lib.render()

    def test_render_sections_joins_back_to_render(self) -> None:
        """快照的分段统计与真正送进模型的字节必须逐字节一致（无额外分隔符）。"""
        lib = PromptLibrary("A\n{{x}}\nB", {"x": "XX"}, name="page")
        sections = lib.render_sections()
        self.assertEqual([s.name for s in sections], ["page", "x", "page"])
        self.assertEqual("".join(s.text for s in sections), lib.render())


class FileAssemblyTests(unittest.TestCase):
    """装配产物 ↔ prompts/*.md 逐字节对账。"""

    @staticmethod
    def _md(name: str) -> str:
        return (_PROMPTS_DIR / name).read_text(encoding="utf-8").strip()

    def _expand(self, consumer: str) -> str:
        """独立复算一遍装配：根页原文里每个槽换成对应资产（已 strip），求值为空
        的动态槽连它前面那条分隔线一起去掉，首尾再 strip。与 catalog 的实现互为
        对照——两边同时写错才可能假通过。"""
        page = (_PROMPTS_DIR / CONSUMERS[consumer]).read_text(encoding="utf-8")
        expanded = SLOT_PATTERN.sub(
            lambda m: self._md(_FILES[m.group(1)])
            if m.group(1) in _FILES
            else "",
            page,
        ).strip()
        return re.sub(r"\n[ \t]*-{3,}[ \t]*\Z", "", expanded).strip()

    def test_planner_render_matches_md_files(self) -> None:
        rendered = build_library("planner").render(scope="group")
        self.assertEqual(rendered, self._expand("planner"))
        # 顺序与内容都真的来自那几份文件
        for name in ("persona.md", "system.md", "envelope.md", "group_chat_rules.md"):
            with self.subTest(name=name):
                self.assertIn(self._md(name), rendered)

    def test_envelope_slot_is_the_file_itself(self) -> None:
        """信封语法的唯一出处是 envelope.md；<replyer-input> 随 Replyer 删除
        （2026-07-31），信封只剩 <agent-input> 一种根元素，新增
        <reply-task-completed> 事件行。"""
        planner_env = build_library("planner").get("envelope")
        self.assertEqual(planner_env, self._md("envelope.md"))
        for tag in ("<agent-input>", "<reply-task-completed>", "<my-reply>"):
            self.assertIn(tag, planner_env)
        self.assertNotIn("<replyer-input>", planner_env)

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

    def test_legacy_voice_asset_is_absent(self) -> None:
        """角色卡的居所只能有一处（persona.md）：voice.md / replyer.md 都不得
        复活——两份都在时 prompt 里会前后各读一遍人格，改一处就当场自相
        矛盾。"""
        self.assertFalse((_PROMPTS_DIR / "voice.md").exists())
        self.assertNotIn("voice", _FILES)
        for filename in CONSUMERS.values():
            text = (_PROMPTS_DIR / filename).read_text(encoding="utf-8")
            for match in SLOT_PATTERN.finditer(text):
                self.assertNotEqual(match.group(1), "voice")

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

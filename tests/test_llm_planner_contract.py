"""Contract tests for LLMPlanner.

Covers (任务与决策契约 §3.1, §3.2):
- happy paths: each action type round-trips through JSON parser
- markdown code fence tolerated
- malformed JSON → fallback IdleAction(llm_json_error:*)
- LLM call raises → fallback IdleAction(llm_call_error:*)
- empty actions → fallback single IdleAction
- unknown action type → fallback IdleAction(llm_schema_error:bad_action)
- no llm client at all → IdleAction(llm_unavailable)

Uses a stub LLM (provides .ainvoke()) injected through the constructor,
so no network/langchain runtime is required.
"""

from __future__ import annotations

import asyncio
import os
import unittest
from types import SimpleNamespace
from typing import Any


def setUpModule() -> None:
    # Prompt 快照与本文件无关；显式钉死关闭——否则服务器 .env 开着
    # PROMPT_SNAPSHOT_ENABLED=true 时，这里每个 decide() 用例都会把测试
    # 请求写进真实快照目录。快照自身的契约测试自管 env，见
    # test_prompt_snapshot_contract.py。
    os.environ["PROMPT_SNAPSHOT_ENABLED"] = "false"

from qqbot.core.time import china_now
from qqbot.services.agent_loop import (
    CallToolAction,
    DecisionContext,
    IdleAction,
    ImageRef,
    LLMPlanner,
    TimelineItem,
)


class _StubLLM:
    def __init__(
        self,
        response_content: str = "",
        raise_exc: Exception | None = None,
    ) -> None:
        self.response_content = response_content
        self.raise_exc = raise_exc
        self.invocations: list[Any] = []

    async def ainvoke(self, messages: Any) -> Any:
        self.invocations.append(messages)
        if self.raise_exc:
            raise self.raise_exc
        return SimpleNamespace(content=self.response_content)


def _ctx() -> DecisionContext:
    return DecisionContext(
        scope_key="group:100",
        correlation_id="CID",
        tick_seq=1,
        now=china_now(),
    )


class LLMPlannerContractTest(unittest.TestCase):
    def test_idle_action_parsed(self) -> None:
        llm = _StubLLM(
            response_content='{"actions":[{"type":"idle","reason":"nothing happening"}]}'
        )
        planner = LLMPlanner(llm_client=llm)
        out = asyncio.run(planner.decide(_ctx()))
        self.assertEqual(len(out.actions), 1)
        self.assertIsInstance(out.actions[0], IdleAction)
        self.assertEqual(out.actions[0].reason, "nothing happening")

    def test_reply_now_parsed_as_call_tool(self) -> None:
        """Reply 不是独立 action：Planner 用 call_tool 落语义 reply_task。
        裸 {"type":"reply"} 会走入 _parse_action 的"未知 type"分支 → IdleAction(bad_action)。
        这条断言把"发言是普通工具"的契约钉死。"""
        body = (
            '{"reasoning":"hi","actions":[{"type":"call_tool",'
            '"tool_name":"reply",'
            '"arguments":{"hold_seconds":8}}]}'
        )
        llm = _StubLLM(response_content=body)
        planner = LLMPlanner(llm_client=llm)
        out = asyncio.run(planner.decide(_ctx()))
        self.assertEqual(len(out.actions), 1)
        self.assertIsInstance(out.actions[0], CallToolAction)
        self.assertEqual(out.actions[0].tool_name, "reply")
        self.assertEqual(out.actions[0].arguments["hold_seconds"], 8)

    def test_bare_reply_type_falls_back_to_idle(self) -> None:
        """旧 {"type":"reply"} 已弃用；planner 把它当作未知 action 处理。"""
        body = (
            '{"actions":[{"type":"reply",'
            '"content":[{"type":"text","data":{"text":"hi"}}],'
            '"target":{"kind":"group","group_id":100}}]}'
        )
        llm = _StubLLM(response_content=body)
        planner = LLMPlanner(llm_client=llm)
        out = asyncio.run(planner.decide(_ctx()))
        self.assertEqual(len(out.actions), 1)
        self.assertIsInstance(out.actions[0], IdleAction)
        self.assertEqual(
            out.actions[0].reason, "llm_schema_error:bad_action"
        )

    def test_code_fence_tolerated(self) -> None:
        body = '```json\n{"actions":[{"type":"idle","reason":"x"}]}\n```'
        llm = _StubLLM(response_content=body)
        planner = LLMPlanner(llm_client=llm)
        out = asyncio.run(planner.decide(_ctx()))
        self.assertIsInstance(out.actions[0], IdleAction)

    def test_bare_code_fence_tolerated(self) -> None:
        body = '```\n{"actions":[{"type":"idle","reason":"y"}]}\n```'
        llm = _StubLLM(response_content=body)
        planner = LLMPlanner(llm_client=llm)
        out = asyncio.run(planner.decide(_ctx()))
        self.assertIsInstance(out.actions[0], IdleAction)

    def test_malformed_json_falls_back_to_idle(self) -> None:
        llm = _StubLLM(response_content="not json at all")
        planner = LLMPlanner(llm_client=llm)
        out = asyncio.run(planner.decide(_ctx()))
        self.assertEqual(len(out.actions), 1)
        self.assertIsInstance(out.actions[0], IdleAction)
        self.assertTrue(out.actions[0].reason.startswith("llm_json_error"))

    def test_llm_call_failure_falls_back_to_idle(self) -> None:
        llm = _StubLLM(raise_exc=RuntimeError("network"))
        planner = LLMPlanner(llm_client=llm)
        out = asyncio.run(planner.decide(_ctx()))
        self.assertIsInstance(out.actions[0], IdleAction)
        self.assertTrue(out.actions[0].reason.startswith("llm_call_error"))

    def test_unknown_action_type_falls_back(self) -> None:
        llm = _StubLLM(
            response_content='{"actions":[{"type":"explode"}]}'
        )
        planner = LLMPlanner(llm_client=llm)
        out = asyncio.run(planner.decide(_ctx()))
        self.assertEqual(
            out.actions[0].reason, "llm_schema_error:bad_action"
        )

    def test_task_lifecycle_parses_as_call_tool_actions(self) -> None:
        body = (
            "{"
            '"actions":['
            '{"type":"call_tool","tool_name":"task",'
            '"arguments":{"action":"create","description":"d","task_ref":"r1"}},'
            '{"type":"call_tool","tool_name":"web","arguments":{"q":"x"},"task_ref":"r1"},'
            '{"type":"call_tool","tool_name":"task",'
            '"arguments":{"action":"complete","task_id":"T1","result_summary":"ok"}},'
            '{"type":"call_tool","tool_name":"task",'
            '"arguments":{"action":"fail","task_id":"T2","reason":"err"}}'
            "]}"
        )
        llm = _StubLLM(response_content=body)
        planner = LLMPlanner(llm_client=llm)
        out = asyncio.run(planner.decide(_ctx()))
        self.assertEqual(len(out.actions), 4)
        for action in out.actions:
            self.assertIsInstance(action, CallToolAction)
        self.assertEqual(out.actions[0].tool_name, "task")
        self.assertEqual(out.actions[0].arguments["task_ref"], "r1")
        self.assertEqual(out.actions[1].arguments, {"q": "x"})

    def test_legacy_task_action_types_fall_back_to_idle(self) -> None:
        for action_type in (
            "create_task",
            "complete_task",
            "fail_task",
            "note_task_progress",
        ):
            with self.subTest(action_type=action_type):
                llm = _StubLLM(
                    response_content=(
                        '{"actions":[{"type":"'
                        + action_type
                        + '"}]}'
                    )
                )
                out = asyncio.run(LLMPlanner(llm_client=llm).decide(_ctx()))
                self.assertIsInstance(out.actions[0], IdleAction)
                self.assertEqual(
                    out.actions[0].reason,
                    "llm_schema_error:bad_action",
                )

    def test_empty_actions_becomes_single_idle(self) -> None:
        llm = _StubLLM(response_content='{"actions":[]}')
        planner = LLMPlanner(llm_client=llm)
        out = asyncio.run(planner.decide(_ctx()))
        self.assertEqual(len(out.actions), 1)
        self.assertIsInstance(out.actions[0], IdleAction)
        self.assertEqual(out.actions[0].reason, "empty_actions")

    def test_actions_not_list_falls_back(self) -> None:
        llm = _StubLLM(
            response_content='{"actions":"not a list","reasoning":"oops"}'
        )
        planner = LLMPlanner(llm_client=llm)
        out = asyncio.run(planner.decide(_ctx()))
        self.assertEqual(len(out.actions), 1)
        self.assertEqual(
            out.actions[0].reason, "llm_schema_error:actions_not_list"
        )
        self.assertEqual(out.reasoning, "oops")

    def test_planner_section_opens_system_prompt(self) -> None:
        """planner.md 打头（页首即人格，随后是系统事实）：信封语法与工具用法
        在系统段之后、行为规范（职责/输出）之前——先给定义再给纪律，输出契约
        收尾（2026-08-01 维护者定稿）。
        "决策引擎"的机器视角开场随 2026-07-31 删除 Replyer 一并退役——Planner
        就是她自己。"""
        llm = _StubLLM(response_content='{"actions":[{"type":"idle","reason":"x"}]}')
        planner = LLMPlanner(llm_client=llm)
        asyncio.run(planner.decide(_ctx()))
        self.assertEqual(len(llm.invocations), 1)
        content = llm.invocations[0][0].content
        self.assertNotIn("决策引擎", content)
        self.assertIn("# 你所处的系统", content)
        self.assertIn("# 你需要做什么", content)
        self.assertIn("输入信封格式规范", content)
        self.assertLess(
            content.index("输入信封格式规范"),
            content.index("# 你需要做什么"),
        )

    def test_planner_carries_the_rules_of_its_own_layer(self) -> None:
        """决策这一环独有的三条纪律只住在 planner.md，没有第三处，掉了就是真
        没有了：念头≠动作、跨拍只能靠任务、一批工具不要重拨。锚点句 2026-08-01
        重钉到维护者的现行措辞（prompt 正文以维护者版本为真相源）。"""
        llm = _StubLLM(response_content='{"actions":[{"type":"idle","reason":"x"}]}')
        planner = LLMPlanner(llm_client=llm)
        rendered = planner._prompt_library.render(scope="group")
        self.assertIn("没有落到时间线上的东西等于没有发生过", rendered)
        self.assertIn("任务是这种断续存在里唯一的连续装置", rendered)
        self.assertIn("想再发一次之前，先看", rendered)
        # system loop 同样要有（页正文不分 scope）
        self.assertIn(
            "任务是这种断续存在里唯一的连续装置",
            planner._prompt_library.render(scope="system"),
        )

    def test_system_mechanics_reach_the_planner(self) -> None:
        """"这个系统怎么转"的唯一出处是 planner.md §你所处的系统
        （2026-07-31 由 system.md 并入）。锚点 2026-08-01 重钉到维护者的
        现行措辞：时间线唯一且只增不改、按拍存在。"""
        from qqbot.services.agent_loop.prompts.catalog import build_library

        planner_text = build_library("planner").render(scope="group")
        for anchor in (
            "这个世界对你而言只有一条时间线",
            "你不是一直醒着的，你以拍为单位存在",
        ):
            with self.subTest(anchor=anchor):
                self.assertIn(anchor, planner_text)

    def test_persona_card_opens_the_planner_page(self) -> None:
        """角色卡就写在 planner.md 页首（2026-07-31 由 persona.md 并入，
        persona / system / group_chat_rules 三个文件槽一并删除）——`persona` /
        `voice` / `disposition` 这些历史段名都不该再作为槽存在，页里只剩
        envelope（信封语法，仍是独立文件）与动态的 tools_usage。

        2026-07-31 删除 Replyer 后 Planner 是卡片唯一的消费者：卡片正文必须
        在。锚点从页里现取（写死原句的话，卡片一改断言就变成假通过），逐句对账
        与"没有第二份副本"在 test_prompt_catalog_contract.LayerBoundaryTests。"""
        llm = _StubLLM(response_content='{"actions":[{"type":"idle","reason":"x"}]}')
        planner = LLMPlanner(llm_client=llm)
        self.assertEqual(
            planner._prompt_library.slot_names(), ["envelope", "tools_usage"]
        )
        from qqbot.services.agent_loop.prompts.catalog import (
            SLOT_PATTERN,
            _PROMPTS_DIR,
        )

        rendered = planner._prompt_library.render(scope="group")
        page = (_PROMPTS_DIR / "planner.md").read_text(encoding="utf-8")
        lines = page.splitlines()
        start = lines.index("# 你是谁") + 1
        end = next(i for i in range(start, len(lines)) if lines[i].startswith("# "))
        anchors = [
            line.strip()
            for line in lines[start:end]
            if len(line.strip()) > 24 and line.strip().startswith("你")
        ]
        self.assertTrue(anchors, "planner.md 人格段没有第二人称锚点，断言会假通过")
        for line in anchors:
            self.assertIn(line, rendered)
        self.assertIsNone(SLOT_PATTERN.search(rendered))

    def test_reply_usage_scoped_without_persona_card(self) -> None:
        """**工具用法文档**里不得抄角色卡正文——卡片确实进 Planner，但走的是
        planner.md 页首那一段这一条路；`tools/*.md` 里再抄一份就是第二个真相
        源，改一处忘另一处当场自相矛盾。"""
        from qqbot.services.agent_loop.tools import build_default_registry

        reg = build_default_registry()
        group_docs = reg.usage_docs("group")
        self.assertIn("## 工具：reply", group_docs)
        self.assertIn("## 工具：send_messages", group_docs)
        # 退役的单数工具没有自己的分段（它是复数名的前缀，按标题行精确匹配）。
        self.assertNotIn("## 工具：send_message\n", group_docs)
        self.assertNotIn("小奏", group_docs)
        system_docs = reg.usage_docs("system")
        self.assertNotIn("## 工具：reply", system_docs)
        self.assertNotIn("## 工具：send_messages", system_docs)
        self.assertNotIn("小奏", system_docs)

    def test_only_tools_usage_is_scope_filtered(self) -> None:
        """scope 这把尺子只作用在 `tools_usage` 上：工具按 allowed_scopes 过滤，
        群专用工具的用法不该泄漏进 system loop。

        分段名单 2026-08-01 重锚：persona / system / group_chat_rules 三个旧
        段名随 2026-07-31 并页进入 planner.md，分段现按「根页正文挂消费者名、
        槽挂槽名」命名，两个 scope 下都只剩 planner 与 envelope。"""
        llm = _StubLLM(response_content='{"actions":[{"type":"idle","reason":"x"}]}')
        planner = LLMPlanner(llm_client=llm)
        group_names = [
            sec.name
            for sec in planner._prompt_library.render_sections(scope="group")
        ]
        system_names = [
            sec.name
            for sec in planner._prompt_library.render_sections(scope="system")
        ]
        for name in ("planner", "envelope"):
            with self.subTest(name=name):
                self.assertIn(name, group_names)
                self.assertIn(name, system_names)
        # 防回潮：并页前的旧段名不得以独立分段复活。
        for retired in ("persona", "system", "group_chat_rules", "disposition"):
            with self.subTest(retired=retired):
                self.assertNotIn(retired, group_names)

    def test_task_note_branch_parsed_as_call_tool(self) -> None:
        body = (
            '{"actions":[{"type":"call_tool","tool_name":"task",'
            '"arguments":{"action":"note","task_id":"T1",'
            '"note":"need to recheck the log"}}]}'
        )
        llm = _StubLLM(response_content=body)
        planner = LLMPlanner(llm_client=llm)
        out = asyncio.run(planner.decide(_ctx()))
        self.assertEqual(len(out.actions), 1)
        action = out.actions[0]
        self.assertIsInstance(action, CallToolAction)
        self.assertEqual(action.tool_name, "task")
        self.assertEqual(action.arguments["action"], "note")
        self.assertEqual(action.arguments["task_id"], "T1")

    def test_task_create_uses_common_triggered_by_event_id(self) -> None:
        body = (
            '{"actions":[{"type":"call_tool","tool_name":"task",'
            '"arguments":{"action":"create","description":"d"},'
            '"triggered_by_event_id":"MSG_42"}]}'
        )
        llm = _StubLLM(response_content=body)
        planner = LLMPlanner(llm_client=llm)
        out = asyncio.run(planner.decide(_ctx()))
        action = out.actions[0]
        self.assertIsInstance(action, CallToolAction)
        self.assertEqual(action.tool_name, "task")
        self.assertEqual(action.triggered_by_event_id, "MSG_42")

    def test_system_prompt_includes_envelope_syntax(self) -> None:
        """信封语法必须注入 system prompt —— LLM 据此读懂 <agent-input> 的标签
        语义。信封语法的唯一出处是 envelope.md（2026-07-31 并页时它是唯一保留
        下来的文件槽），本用例锚定文档头和几个关键标签即可，避免绑死文案。"""
        llm = _StubLLM(
            response_content='{"actions":[{"type":"idle","reason":"x"}]}'
        )
        planner = LLMPlanner(llm_client=llm)
        asyncio.run(planner.decide(_ctx()))
        content = llm.invocations[0][0].content

        # envelope.md 文档头（信封语法的唯一出处）
        self.assertIn("输入信封格式规范", content)
        # 容器与事件行都必须逐个规定过
        self.assertIn("<tool-catalog>", content)
        self.assertIn("<active-tasks>", content)
        self.assertIn("<timeline>", content)
        self.assertIn("<time>", content)
        self.assertIn("<my-reply>", content)
        self.assertNotIn("<my-thought", content)
        # 2026-07-02 起不再有 pending-tool-results 区（工具结果只在 timeline
        # 单点呈现，防双重渲染诱发复读）——文档不得再登记这个标签
        self.assertNotIn("<pending-tool-results>", content)
        # 特殊标记
        self.assertIn("<truncated/>", content)
        self.assertIn("<processing/>", content)
        # 两态语义：status 只表示是否结束，成败在子元素
        self.assertIn("只表示该调用是否已结束", content)
        # 输出侧的动作形状在 planner.md（代码不下发 schema，删了就没有第二处）
        self.assertIn('"type":"call_tool"', content)

    def test_system_prompt_teaches_two_step_speaking(self) -> None:
        """发言两步的红线（2026-07-31 删除 Replyer）：reply 不承载最终字句、
        只存解析并等待；措辞发生在 send_messages；该工具调用自身的逐气泡回执
        是现行发送事实，<my-reply> 仅表示旧链路记录。三处正文各钉一句——
        planner.md 的运行段、tools/reply.md 与 tools/send_messages.md 的接口页
        （后两者随 tools_usage 段进 Planner prompt，所以本用例必须带工具注册表
        装配）。"""
        from qqbot.services.agent_loop.tools import build_default_registry

        llm = _StubLLM(
            response_content='{"actions":[{"type":"idle","reason":"x"}]}'
        )
        planner = LLMPlanner(
            llm_client=llm, tool_registry=build_default_registry()
        )
        asyncio.run(planner.decide(_ctx()))
        content = llm.invocations[0][0].content

        # planner.md：开口被拆成两步、完成唤醒既不是命令也不是许可
        self.assertIn("开口被拆成两步", content)
        self.assertIn("它既不是要你说话的命令", content)
        # tools/reply.md：reply 不承载消息正文；tools/send_messages.md：调用行
        # 自身的逐条回执是发言事实（不派生 <my-reply>）。
        self.assertIn("该工具不发送消息", content)
        self.assertIn("逐气泡回执构成发送记录", content)
        # uncertain 客观标明对应气泡可能已送达。
        self.assertIn("对应气泡可能已经送达", content)
        # 信封段这一侧只留客观定义（<my-reply> 现为仅旧记录的行）
        self.assertIn("其中成功的子元素即实际到达 QQ 的内容", content)

    def test_default_prompt_section_order(self) -> None:
        """段序按根页写定的顺序：人格 < 系统 < 信封 < tools_usage
        < 职责 < 输出。LLM 先读"你是谁→处在哪里→输入长什么样→手里有什么"，
        再读"要做什么→怎么输出"——输出契约离真实输入最近
        （2026-08-01 维护者定稿）。"""
        from qqbot.services.agent_loop.tool_registry import ToolRegistry

        class _StubTool:
            name = "stub_tool_for_order"
            description = "..."
            arguments_schema = {"type": "object"}
            usage_prompt = "STUB-TOOL-ORDER-MARKER content"

            async def run(self, arguments: dict, **_: object) -> dict:
                return {}

        reg = ToolRegistry()
        reg.register(_StubTool())

        llm = _StubLLM(
            response_content='{"actions":[{"type":"idle","reason":"x"}]}'
        )
        planner = LLMPlanner(
            llm_client=llm,
            tool_registry=reg,
        )
        asyncio.run(planner.decide(_ctx()))
        content = llm.invocations[0][0].content

        # 两个槽的先后：系统段之后依次是信封与工具用法，随后才是行为规范。
        self.assertEqual(
            planner._prompt_library.slot_names(), ["envelope", "tools_usage"]
        )
        # 段序用段标题对账，不拿会迭代的正文措辞当排序锚点。
        idx_persona = content.index("# 你是谁")
        idx_system = content.index("# 你所处的系统")
        idx_envelope = content.index("输入信封格式规范")
        idx_tools = content.index("STUB-TOOL-ORDER-MARKER")
        idx_purpose = content.index("# 你需要做什么")
        idx_output = content.index("# 你的输出")

        self.assertEqual(
            [
                idx_persona,
                idx_system,
                idx_envelope,
                idx_tools,
                idx_purpose,
                idx_output,
            ],
            sorted(
                [
                    idx_persona,
                    idx_system,
                    idx_envelope,
                    idx_tools,
                    idx_purpose,
                    idx_output,
                ]
            ),
        )

    def test_reply_tool_usage_doc_renders_via_tool_registry(self) -> None:
        """ReplyTool.usage_prompt 必须随 registry 进入 Planner prompt。"""
        from qqbot.services.agent_loop.tools import build_default_registry

        # 工具无构造依赖；usage_docs 只读 usage_prompt，不触发任何运行期依赖
        reg = build_default_registry()

        llm = _StubLLM(
            response_content='{"actions":[{"type":"idle","reason":"x"}]}'
        )
        planner = LLMPlanner(llm_client=llm, tool_registry=reg)
        asyncio.run(planner.decide(_ctx()))
        content = llm.invocations[0][0].content

        # 按工具名分段的标题：发言两步各一段
        self.assertIn("## 工具：reply", content)
        self.assertIn("## 工具：send_messages", content)
        # 锚点随 reply.md 的现行接口语义：保存分析并等待，不直接发送。
        self.assertIn("保存当前 scope 的完整会话分析并启动短时等待", content)
        self.assertIn("成功仅表示修订已保存并进入等待状态", content)
        # 退役的单数 send_message 不得再有自己的分段（注意它是复数名的前缀，
        # 用段标题加换行精确匹配）。
        self.assertNotIn("## 工具：send_message\n", content)

    def test_system_prompt_includes_tool_usage_docs(self) -> None:
        """Tool 的 sibling .md 必须按工具名分段注入 system prompt，
        新增/下架工具时自动随 ToolRegistry 一起出现/消失。"""
        from qqbot.services.agent_loop.tool_registry import ToolRegistry

        class _StubTool:
            name = "stub_tool"
            description = "stub for tests"
            arguments_schema = {"type": "object"}
            usage_prompt = "STUB-TOOL-USAGE-MARKER: only-emitted-when-registered"

            async def run(self, arguments: dict, **_: object) -> dict:
                return {}

        reg = ToolRegistry()
        reg.register(_StubTool())

        llm = _StubLLM(
            response_content='{"actions":[{"type":"idle","reason":"x"}]}'
        )
        planner = LLMPlanner(llm_client=llm, tool_registry=reg)
        asyncio.run(planner.decide(_ctx()))
        content = llm.invocations[0][0].content

        self.assertIn("## 工具：stub_tool", content)
        self.assertIn("STUB-TOOL-USAGE-MARKER", content)

    def test_system_prompt_skips_tool_without_usage_prompt(self) -> None:
        """没写 sibling .md 的工具不应在 system prompt 里产生孤儿
        `## 工具：foo` 空标题。"""
        from qqbot.services.agent_loop.tool_registry import ToolRegistry

        class _NoUsageTool:
            name = "no_usage_tool"
            description = "stub"
            arguments_schema = {"type": "object"}
            # 故意不设 usage_prompt

            async def run(self, arguments: dict, **_: object) -> dict:
                return {}

        reg = ToolRegistry()
        reg.register(_NoUsageTool())

        llm = _StubLLM(
            response_content='{"actions":[{"type":"idle","reason":"x"}]}'
        )
        planner = LLMPlanner(llm_client=llm, tool_registry=reg)
        asyncio.run(planner.decide(_ctx()))
        content = llm.invocations[0][0].content

        self.assertNotIn("## 工具：no_usage_tool", content)

    def test_custom_prompt_library_overrides_default(self) -> None:
        """传入自定义提示词库时绕过默认装配 —— 调用方拥有最终拼接权。"""
        from qqbot.services.agent_loop.prompts.catalog import PromptLibrary

        custom = PromptLibrary("CUSTOM-ONLY-MARKER")

        llm = _StubLLM(
            response_content='{"actions":[{"type":"idle","reason":"x"}]}'
        )
        planner = LLMPlanner(
            llm_client=llm,
            prompt_library=custom,
        )
        asyncio.run(planner.decide(_ctx()))
        content = llm.invocations[0][0].content

        self.assertEqual(content, "CUSTOM-ONLY-MARKER")

    def test_system_prompt_is_task_centric(self) -> None:
        """新协议要求 LLM 围绕 active tasks 决策；这里只验证关键约束词出现，
        不绑定文案细节（避免无谓脆弱）。"""
        from qqbot.services.agent_loop.tools import build_default_registry

        llm = _StubLLM(
            response_content='{"actions":[{"type":"idle","reason":"x"}]}'
        )
        planner = LLMPlanner(
            llm_client=llm,
            tool_registry=build_default_registry(),
        )
        asyncio.run(planner.decide(_ctx()))
        content = llm.invocations[0][0].content

        self.assertIn("<active-tasks>", content)
        self.assertIn("## 工具：task", content)
        # 收束记法锚点钉 tools/task.md 现行 JSON 写法（XML 属性记法已退役）。
        self.assertIn('"action":"complete"', content)
        self.assertIn('"action":"fail"', content)
        self.assertNotIn('"type":"complete_task"', content)
        self.assertNotIn('"type":"fail_task"', content)
        # 必须明示"新消息不会自动取消 task"（锚点为维护者现行措辞）
        self.assertIn("别的事不会替你关掉它", content)

    def test_human_message_is_plain_text_never_multimodal(self) -> None:
        """2026-07-28：Planner 是纯文本模型。timeline 里带已落盘图片的消息
        **不再**让 HumanMessage.content 变成 block 数组 —— 图片语义经 ingest
        期写好的 desc= 属性随 render 文本抵达（见 image_description 模块），
        像素永不进 Planner 的 prompt，也就没有旧路径那套「↓ image hash= label
        + base64」的对位约定了。

        local_path 指向不存在的文件：正是要证明**根本没有读盘**这一步。"""
        ctx = DecisionContext(
            scope_key="group:100",
            correlation_id="CID",
            tick_seq=1,
            now=china_now(),
            timeline=[
                TimelineItem(
                    event_id="E1",
                    occurred_at=china_now(),
                    kind="message",
                    render=(
                        '<message>hi <image hash="h1" '
                        'desc="一张终端截图，文字内容：ImportError"/></message>'
                    ),
                    images=[
                        ImageRef(
                            file_hash="h1",
                            local_path="/nonexistent/never-read",
                            mime="image/png",
                        )
                    ],
                ),
            ],
        )
        llm = _StubLLM(
            response_content='{"actions":[{"type":"idle","reason":"x"}]}'
        )
        planner = LLMPlanner(llm_client=llm)
        out = asyncio.run(planner.decide(ctx))
        self.assertIsInstance(out.actions[0], IdleAction)

        human_content = llm.invocations[0][1].content
        self.assertIsInstance(human_content, str)
        self.assertNotIn("base64", human_content)
        self.assertNotIn("image_url", human_content)
        # 描述本身必须原样进 prompt —— 它是模型看到这张图的唯一途径。
        self.assertIn(
            'desc="一张终端截图，文字内容：ImportError"', human_content
        )

    def test_bot_user_id_rendered_as_agent_input_attribute(self) -> None:
        """DecisionContext.bot_user_id 必须以 bot_qq= 出现在 <agent-input> 的
        attribute 里。LLM 据此对照 <at qq="..."/> 判断是否在叫它。"""
        ctx = DecisionContext(
            scope_key="group:100",
            correlation_id="CID",
            tick_seq=1,
            now=china_now(),
            bot_user_id="3167291813",
        )
        llm = _StubLLM(
            response_content='{"actions":[{"type":"idle","reason":"x"}]}'
        )
        planner = LLMPlanner(llm_client=llm)
        asyncio.run(planner.decide(ctx))
        human_text = llm.invocations[0][1].content
        self.assertIn('bot_qq="3167291813"', human_text)
        # scope/now 也仍在（@tick 已于 2026-07-30 从信封删除）
        self.assertIn('scope="group:100"', human_text)
        self.assertIn("<current now=", human_text)

    def test_no_bot_user_id_omits_attribute(self) -> None:
        """bot_user_id 为 None 时不渲染 bot_qq= 属性 —— prompt 体积稳定，
        LLM 知道这是降级场景（启动初期 napcat 还没连上）。"""
        ctx = DecisionContext(
            scope_key="group:100",
            correlation_id="CID",
            tick_seq=1,
            now=china_now(),
        )
        self.assertIsNone(ctx.bot_user_id)
        llm = _StubLLM(
            response_content='{"actions":[{"type":"idle","reason":"x"}]}'
        )
        planner = LLMPlanner(llm_client=llm)
        asyncio.run(planner.decide(ctx))
        human_text = llm.invocations[0][1].content
        self.assertNotIn("bot_qq", human_text)

    def test_agent_input_now_always_rendered_in_china_timezone(self) -> None:
        """即便 caller 传入 UTC datetime，<agent-input now="..."> 也必须
        渲染为 +08:00 —— 时区契约：暴露给 LLM 的所有时间都是北京时间。"""
        from datetime import datetime, timezone

        utc_now = datetime(2026, 5, 28, 1, 55, 46, tzinfo=timezone.utc)
        ctx = DecisionContext(
            scope_key="group:100",
            correlation_id="CID",
            tick_seq=1,
            now=utc_now,
        )
        llm = _StubLLM(
            response_content='{"actions":[{"type":"idle","reason":"x"}]}'
        )
        planner = LLMPlanner(llm_client=llm)
        asyncio.run(planner.decide(ctx))
        human_text = llm.invocations[0][1].content
        # UTC 01:55 → 北京 09:55 +08:00
        self.assertIn('now="2026-05-28T09:55:46+08:00"', human_text)
        self.assertNotIn("+00:00", human_text)

    def test_bot_role_rendered_as_agent_input_attribute(self) -> None:
        """DecisionContext.bot_role 出现在 <agent-input> 属性里，让 LLM 知道
        自己是 owner / admin / member。"""
        ctx = DecisionContext(
            scope_key="group:100",
            correlation_id="CID",
            tick_seq=1,
            now=china_now(),
            bot_role="admin",
        )
        llm = _StubLLM(response_content='{"actions":[{"type":"idle","reason":"x"}]}')
        planner = LLMPlanner(llm_client=llm)
        asyncio.run(planner.decide(ctx))
        human_text = llm.invocations[0][1].content
        self.assertIn('bot_role="admin"', human_text)

    def test_no_bot_role_omits_attribute(self) -> None:
        ctx = DecisionContext(
            scope_key="group:100",
            correlation_id="CID",
            tick_seq=1,
            now=china_now(),
        )
        self.assertIsNone(ctx.bot_role)
        llm = _StubLLM(response_content='{"actions":[{"type":"idle","reason":"x"}]}')
        planner = LLMPlanner(llm_client=llm)
        asyncio.run(planner.decide(ctx))
        human_text = llm.invocations[0][1].content
        self.assertNotIn("bot_role=", human_text)

    def test_tool_permission_metadata_rendered_in_catalog(self) -> None:
        """tool_catalog 里 required_permission / required_bot_role 必须出现在
        每条 <tool> 标签的属性上 —— LLM 据此判断"我能调谁"。"""
        from qqbot.core.permissions import PermissionTier
        from qqbot.services.agent_loop.tool_registry import ToolRegistry

        class _KickTool:
            name = "kick_member"
            description = "kick a member"
            arguments_schema = {"type": "object"}
            required_permission = PermissionTier.ADMIN
            require_bot_admin = True

            async def run(self, arguments: dict, **_: Any) -> Any:
                return {}

        registry = ToolRegistry()
        registry.register(_KickTool())

        ctx = DecisionContext(
            scope_key="group:100",
            correlation_id="CID",
            tick_seq=1,
            now=china_now(),
        )
        llm = _StubLLM(response_content='{"actions":[{"type":"idle","reason":"x"}]}')
        planner = LLMPlanner(llm_client=llm, tool_registry=registry)
        asyncio.run(planner.decide(ctx))
        human_text = llm.invocations[0][1].content
        self.assertIn('name="kick_member"', human_text)
        self.assertIn('required_permission="ADMIN"', human_text)
        # _KickTool 用旧字段 require_bot_admin=True，经 get_tool_required_bot_role
        # 回退渲染成 required_bot_role="admin"（验证新旧字段兼容打通）。
        self.assertIn('required_bot_role="admin"', human_text)

    def test_call_tool_action_parses_triggered_by_event_id(self) -> None:
        """LLM 在 call_tool 上填 triggered_by_event_id 时必须解到
        CallToolAction.triggered_by_event_id。"""
        llm = _StubLLM(
            response_content=(
                '{"actions":[{"type":"call_tool","tool_name":"send_message",'
                '"arguments":{},"triggered_by_event_id":"E_msg_77"}]}'
            )
        )
        planner = LLMPlanner(llm_client=llm)
        out = asyncio.run(planner.decide(_ctx()))
        from qqbot.services.agent_loop.decision import CallToolAction

        self.assertEqual(len(out.actions), 1)
        action = out.actions[0]
        self.assertIsInstance(action, CallToolAction)
        assert isinstance(action, CallToolAction)
        self.assertEqual(action.triggered_by_event_id, "E_msg_77")

    def test_call_tool_without_triggered_by_defaults_to_none(self) -> None:
        llm = _StubLLM(
            response_content=(
                '{"actions":[{"type":"call_tool","tool_name":"send_message","arguments":{}}]}'
            )
        )
        planner = LLMPlanner(llm_client=llm)
        out = asyncio.run(planner.decide(_ctx()))
        from qqbot.services.agent_loop.decision import CallToolAction

        action = out.actions[0]
        assert isinstance(action, CallToolAction)
        self.assertIsNone(action.triggered_by_event_id)

    def test_no_llm_client_returns_unavailable_idle(self) -> None:
        # 直接 stub _ensure_llm 返回 None，不依赖任何真实配置——这条验的是
        # "拿不到 LLM 时降级成 idle(llm_unavailable)"，而拿不到的原因（缺
        # config/model_providers.json、解析失败、role 无候选）不在本例范围内。
        planner = LLMPlanner(llm_client=None)

        async def _no_llm() -> Any:
            return None

        planner._ensure_llm = _no_llm  # type: ignore[assignment]
        out = asyncio.run(planner.decide(_ctx()))
        self.assertIsInstance(out.actions[0], IdleAction)
        self.assertEqual(out.actions[0].reason, "llm_unavailable")


# 2026-07-02：<pending-tool-results> 区已删除（工具结果只在 timeline 的
# <tool-call> 行呈现一次），planner 侧的 _render_tool_result_xml 随之移除。
# 失败 <error> 的结构化属性渲染契约由 test_agent_loop_projection_contract 的
# timeline 渲染用例继续把守（fold 层 + <tool-call> 渲染层双覆盖）。


class EnvelopeReasoningIsolationTests(unittest.TestCase):
    """<validation-error> 仍进重试输入；reasoning 不进入跨拍信封。"""

    def _render_with(self, **overrides: Any) -> str:
        from dataclasses import replace

        llm = _StubLLM(
            response_content='{"actions":[{"type":"idle","reason":"x"}]}'
        )
        planner = LLMPlanner(llm_client=llm)
        ctx = replace(_ctx(), **overrides)
        asyncio.run(planner.decide(ctx))
        return llm.invocations[0][1].content

    def test_last_reasoning_block_removed(self) -> None:
        # DecisionContext 已无 last_reasoning 字段，信封任何情况下都不得再
        # 出现 <last-reasoning> 区块。
        from qqbot.services.agent_loop.decision import DecisionContext

        xml = self._render_with()
        self.assertNotIn("<last-reasoning", xml)
        self.assertNotIn("<validation-error", xml)
        self.assertFalse(hasattr(_ctx(), "last_reasoning"))
        self.assertFalse(hasattr(DecisionContext, "last_reasoning"))

    def test_prompt_does_not_register_a_reasoning_history_row(self) -> None:
        llm = _StubLLM(
            response_content='{"actions":[{"type":"idle","reason":"x"}]}'
        )
        planner = LLMPlanner(llm_client=llm)
        rendered = planner._prompt_library.render(scope="group")
        self.assertNotIn("<my-thought", rendered)
        self.assertIn("不回显到后续输入", rendered)

    def test_validation_feedback_rendered_on_retry_context(self) -> None:
        xml = self._render_with(
            validation_feedback="attempt 1 rejected: idle_with_other_actions"
        )
        self.assertIn("<validation-error>", xml)
        self.assertIn("idle_with_other_actions", xml)


class _SequenceLLM:
    """按序返回多个响应的 stub——覆盖解析失败重试链路。"""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.invocations: list[Any] = []

    async def ainvoke(self, messages: Any) -> Any:
        self.invocations.append(messages)
        content = self._responses[min(len(self.invocations) - 1, len(self._responses) - 1)]
        return SimpleNamespace(content=content)


class JsonParseRetryTests(unittest.TestCase):
    """契约 §7.1（2026-07-02 落地）：JSON 不可解析时 planner 内重试至多 2 次
    （共 3 次调用），重试消息携带原始输出 + 解析错误；全败才回退 idle。"""

    def test_bad_then_good_json_recovers(self) -> None:
        llm = _SequenceLLM([
            "呃，我想想……（不是 JSON）",
            '{"actions":[{"type":"idle","reason":"fixed"}]}',
        ])
        planner = LLMPlanner(llm_client=llm)
        out = asyncio.run(planner.decide(_ctx()))
        self.assertEqual(len(llm.invocations), 2)
        self.assertIsInstance(out.actions[0], IdleAction)
        self.assertEqual(out.actions[0].reason, "fixed")
        # 重试对话必须带回原始输出（AIMessage）+ 纠错指令（HumanMessage）
        retry_messages = llm.invocations[1]
        self.assertGreater(len(retry_messages), len(llm.invocations[0]))
        tail_texts = [
            str(getattr(m, "content", "")) for m in retry_messages[-2:]
        ]
        self.assertIn("不是 JSON", tail_texts[0])
        self.assertIn("valid JSON", tail_texts[1])

    def test_persistent_bad_json_gives_up_after_three(self) -> None:
        llm = _SequenceLLM(["x", "y", "z"])
        planner = LLMPlanner(llm_client=llm)
        out = asyncio.run(planner.decide(_ctx()))
        self.assertEqual(len(llm.invocations), 3)
        self.assertIsInstance(out.actions[0], IdleAction)
        self.assertTrue(str(out.actions[0].reason).startswith("llm_json_error"))


class _RoutedSequenceLLM(_SequenceLLM):
    """带 mark_last_call_failed 的 stub——模拟 RoutedChatModel 的体级失败回报口。"""

    def __init__(self, responses: list[str]) -> None:
        super().__init__(responses)
        self.body_failures: list[str] = []

    def mark_last_call_failed(self, reason: str) -> str | None:
        self.body_failures.append(reason)
        return "stub/endpoint"


class BodyRejectedReportTests(unittest.TestCase):
    """LLM 路由契约 §4（2026-08-02）：正文不是 JSON 时 planner 把这次调用回报
    成端点体级失败——路由层只把异常算失败，不回报就会让同一拍的三次重试反复
    打在同一个端点上（上游内容策略拦截时实测如此）。回报口缺失（老 stub /
    注入的裸客户端）必须静默跳过，不影响重试与降级。"""

    def test_each_parse_failure_reports_body_rejection(self) -> None:
        llm = _RoutedSequenceLLM([
            "被内容策略拦了（不是 JSON）",
            '{"actions":[{"type":"idle","reason":"fixed"}]}',
        ])
        out = asyncio.run(LLMPlanner(llm_client=llm).decide(_ctx()))
        self.assertEqual(out.actions[0].reason, "fixed")
        self.assertEqual(len(llm.body_failures), 1)
        self.assertTrue(llm.body_failures[0].startswith("json_error:"))

    def test_final_failure_is_reported_too(self) -> None:
        """给下一拍留下冷却态：本拍降级 idle 后，下一拍从别的端点起步。"""
        llm = _RoutedSequenceLLM(["x", "y", "z"])
        out = asyncio.run(LLMPlanner(llm_client=llm).decide(_ctx()))
        self.assertTrue(str(out.actions[0].reason).startswith("llm_json_error"))
        self.assertEqual(len(llm.body_failures), 3)

    def test_successful_parse_reports_nothing(self) -> None:
        llm = _RoutedSequenceLLM(['{"actions":[{"type":"idle","reason":"ok"}]}'])
        asyncio.run(LLMPlanner(llm_client=llm).decide(_ctx()))
        self.assertEqual(llm.body_failures, [])

    def test_client_without_report_hook_is_skipped(self) -> None:
        llm = _SequenceLLM(["x", "y", "z"])
        out = asyncio.run(LLMPlanner(llm_client=llm).decide(_ctx()))
        self.assertEqual(len(llm.invocations), 3)
        self.assertTrue(str(out.actions[0].reason).startswith("llm_json_error"))

    def test_report_hook_exception_never_breaks_decision(self) -> None:
        class _Exploding(_RoutedSequenceLLM):
            def mark_last_call_failed(self, reason: str) -> str | None:
                raise RuntimeError("router bookkeeping crashed")

        llm = _Exploding([
            "不是 JSON",
            '{"actions":[{"type":"idle","reason":"fixed"}]}',
        ])
        out = asyncio.run(LLMPlanner(llm_client=llm).decide(_ctx()))
        self.assertEqual(out.actions[0].reason, "fixed")


class SavedMemesEnvelopeTests(unittest.TestCase):
    """<saved-memes> 渲染契约（表情包工具黑盒设计 §prompt 注入）：
    有收藏才渲染整段；每条 <meme> 带 hash / saved_at 属性 + 描述正文
    （XML 转义）；位置在 </timeline> 之后、<active-tasks> 之前（2026-08-01
    显著性移位——选图发生在读完局面之后，目录须紧邻决策位置；缓存代价
    见 EnvelopeCacheLayoutTests）。"""

    def _envelope_text(self, ctx: DecisionContext) -> str:
        llm = _StubLLM(
            response_content='{"actions":[{"type":"idle","reason":"x"}]}'
        )
        planner = LLMPlanner(llm_client=llm)
        asyncio.run(planner.decide(ctx))
        return llm.invocations[0][1].content

    def _ctx_with_memes(self, description: str) -> DecisionContext:
        from qqbot.services.agent_loop import MemeView

        return DecisionContext(
            scope_key="group:100",
            correlation_id="CID",
            tick_seq=1,
            now=china_now(),
            saved_memes=[
                MemeView(
                    file_hash="ab" * 32,
                    description=description,
                    saved_at=china_now(),
                )
            ],
        )

    def test_saved_memes_rendered_between_timeline_and_tasks(self) -> None:
        text = self._envelope_text(
            self._ctx_with_memes("黑猫瞪眼，配字就这，嘲讽用")
        )
        self.assertIn("<saved-memes>", text)
        self.assertIn(f'<meme hash="{"ab" * 32}"', text)
        self.assertIn('saved_at="', text)
        self.assertIn("黑猫瞪眼，配字就这，嘲讽用", text)
        # 2026-08-01 显著性移位：timeline → memes → active-tasks
        self.assertLess(
            text.index("</timeline>"), text.index("<saved-memes>")
        )
        self.assertLess(
            text.index("</saved-memes>"), text.index("<active-tasks>")
        )

    def test_no_saved_memes_omits_section(self) -> None:
        # 空收藏整段省略——不渲染空 <saved-memes>。
        text = self._envelope_text(_ctx())
        self.assertNotIn("<saved-memes>", text)

    def test_description_xml_escaped(self) -> None:
        text = self._envelope_text(self._ctx_with_memes("A<B&C"))
        self.assertIn("A&lt;B&amp;C", text)
        self.assertNotIn(">A<B&C<", text)


class PendingReplySectionRemovedTests(unittest.TestCase):
    """`<pending-reply>` 段已于 2026-07-24 删除（待办清单#19）。

    它的每个字段都被 timeline 上的 `<tool-call name="reply">` 行逐字段覆盖
    （reply_task_id / revision / flush_at / hard_deadline 在 `<result>` 里，
    hold_seconds 在 `<args>` 里）。reply 成功行不再折叠之后，独立状态区就是
    重复渲染，一并撤掉；顺带撤掉了信封里变化最频繁的那一段（每次续期都变、
    创建/到点时整段出现消失），`</active-tasks>` 到 `<current/>` 之间不再有
    缓存抖动源。
    """

    def test_envelope_has_no_pending_reply_section(self) -> None:
        ctx = DecisionContext(
            scope_key="group:100",
            correlation_id="CID",
            tick_seq=2,
            now=china_now(),
        )
        llm = _StubLLM(
            response_content='{"actions":[{"type":"idle","reason":"x"}]}'
        )
        planner = LLMPlanner(llm_client=llm)
        asyncio.run(planner.decide(ctx))
        text = llm.invocations[0][1].content
        self.assertNotIn("<pending-reply", text)
        # </active-tasks> 之后直接就是尾部时钟字段
        self.assertLess(
            text.index("</active-tasks>"), text.index("<current now=")
        )

    def test_decision_context_has_no_pending_reply_field(self) -> None:
        """防回潮：字段本身也已删除，不能靠 getattr 兜底悄悄复活。"""
        self.assertNotIn(
            "pending_reply", DecisionContext.__dataclass_fields__
        )


class EnvelopeCacheLayoutTests(unittest.TestCase):
    """信封段序与前缀稳定性契约（2026-07-12，前缀缓存）。

    OpenAI 系 API 的自动前缀缓存要求前缀**逐字节一致**：每拍必变的 now
    不得出现在信封头部（否则缓存前缀在 system prompt 末尾就断掉，timeline
    每拍全价重计费），段序按变化频率升序：tool-catalog → timeline →
    saved-memes → active-tasks → <current/> → validation-error。saved-memes
    少变、本应在 timeline 之前，2026-08-01 为选图显著性移到其后（唯一例外，
    代价是随 timeline 追加逐拍重编码，见 SavedMemesEnvelopeTests）。原
    pending-reply 段已于 2026-07-24 删除，@tick 已于 2026-07-30 删除。改动
    信封布局前必须先想清对缓存前缀的影响——本类是回归防线。"""

    def _render(self, ctx: DecisionContext) -> str:
        llm = _StubLLM(
            response_content='{"actions":[{"type":"idle","reason":"x"}]}'
        )
        planner = LLMPlanner(llm_client=llm)
        asyncio.run(planner.decide(ctx))
        return llm.invocations[0][1].content

    def test_agent_input_head_has_no_per_tick_attributes(self) -> None:
        text = self._render(_ctx())
        head = text[: text.index(">") + 1]  # <agent-input ...> 开标签
        self.assertTrue(head.startswith("<agent-input"))
        self.assertIn('scope="group:100"', head)
        self.assertNotIn("now=", head)
        self.assertNotIn("tick=", head)

    def test_current_element_carries_clock_after_tasks(self) -> None:
        text = self._render(_ctx())
        self.assertIn("<current now=", text)
        # @tick 于 2026-07-30 从信封删除（tick_seq 本身保留，见事件 payload /
        # 日志 / 快照）：它与 now 同处 <current/>，缓存收益恒为零，而对模型
        # 无锚点、重启后 tick="1" 配满窗历史属误导。这里断言**整个信封**都
        # 不再出现拍号——加回来必须是带锚点的 burst_step，不是它。
        self.assertNotIn("tick=", text)
        # 段序：timeline → active-tasks → <current/>
        self.assertLess(
            text.index("</timeline>"), text.index("<active-tasks>")
        )
        self.assertLess(
            text.index("</active-tasks>"), text.index("<current now=")
        )

    def test_validation_error_rendered_after_current(self) -> None:
        from dataclasses import replace

        text = self._render(
            replace(_ctx(), validation_feedback="attempt 1 rejected: x")
        )
        self.assertLess(
            text.index("<current now="), text.index("<validation-error>")
        )

    def test_prefix_stable_across_ticks(self) -> None:
        """同一 timeline、不同 now 的两拍，<current/> 之前的信封文本必须逐
        字节一致——这是前缀缓存能命中的直接判据。tick_seq 仍一并改动：@tick
        已于 2026-07-30 从信封删除，这里顺带钉住它不会重新漏进前缀。"""
        from dataclasses import replace
        from datetime import timedelta

        base = _ctx()
        text_a = self._render(base)
        text_b = self._render(
            replace(base, tick_seq=2, now=base.now + timedelta(seconds=47))
        )
        prefix_a = text_a[: text_a.index("<current ")]
        prefix_b = text_b[: text_b.index("<current ")]
        self.assertEqual(prefix_a, prefix_b)


if __name__ == "__main__":
    unittest.main()

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
import unittest
from types import SimpleNamespace
from typing import Any

from qqbot.core.time import china_now
from qqbot.services.agent_loop import (
    CallToolAction,
    CompleteTaskAction,
    CreateTaskAction,
    DecisionContext,
    FailTaskAction,
    IdleAction,
    ImageRef,
    LLMPlanner,
    NoteTaskProgressAction,
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
        """Reply 不再是独立 action：LLM 必须用 call_tool tool_name=send_message 发言。
        裸 {"type":"reply"} 会走入 _parse_action 的"未知 type"分支 → IdleAction(bad_action)。
        这条断言把"发言是普通工具"的契约钉死。"""
        body = (
            '{"reasoning":"hi","actions":[{"type":"call_tool",'
            '"tool_name":"send_message",'
            '"arguments":{"content":[{"type":"text","data":{"text":"hi"}}],'
            '"target":{"kind":"group","group_id":100}}}]}'
        )
        llm = _StubLLM(response_content=body)
        planner = LLMPlanner(llm_client=llm)
        out = asyncio.run(planner.decide(_ctx()))
        self.assertEqual(len(out.actions), 1)
        self.assertIsInstance(out.actions[0], CallToolAction)
        self.assertEqual(out.actions[0].tool_name, "send_message")
        self.assertEqual(
            out.actions[0].arguments["target"],
            {"kind": "group", "group_id": 100},
        )

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

    def test_all_action_types_parsed(self) -> None:
        body = (
            "{"
            '"actions":['
            '{"type":"create_task","description":"d","related_tools":["t"],"task_ref":"r1"},'
            '{"type":"call_tool","tool_name":"web","arguments":{"q":"x"},"task_ref":"r1"},'
            '{"type":"complete_task","task_id":"T1","result_summary":"ok"},'
            '{"type":"fail_task","task_id":"T2","reason":"err"}'
            "]}"
        )
        llm = _StubLLM(response_content=body)
        planner = LLMPlanner(llm_client=llm)
        out = asyncio.run(planner.decide(_ctx()))
        self.assertEqual(len(out.actions), 4)
        self.assertIsInstance(out.actions[0], CreateTaskAction)
        self.assertIsInstance(out.actions[1], CallToolAction)
        self.assertIsInstance(out.actions[2], CompleteTaskAction)
        self.assertIsInstance(out.actions[3], FailTaskAction)
        self.assertEqual(out.actions[0].task_ref, "r1")
        self.assertEqual(out.actions[1].arguments, {"q": "x"})

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

    def test_identity_section_opens_system_prompt(self) -> None:
        """identity.md 打头：先立"决策引擎操作一个 QQ 账号"的机器视角。
        决策层（规划/任务/工具选择/reasoning）无人格；协议段在其后。"""
        llm = _StubLLM(response_content='{"actions":[{"type":"idle","reason":"x"}]}')
        planner = LLMPlanner(llm_client=llm)
        asyncio.run(planner.decide(_ctx()))
        self.assertEqual(len(llm.invocations), 1)
        content = llm.invocations[0][0].content
        self.assertIn("decision engine", content)
        self.assertIn("tasks persist, conversation flows around them", content)
        self.assertLess(
            content.index("decision engine"),
            content.index("tasks persist, conversation flows around them"),
        )

    def test_no_persona_section_registered(self) -> None:
        """默认装配不再有独立 persona 段——人格只存在于 send_message 的
        用法文档（tools/send_message.md 的 Voice 节），随 tools_usage 进 prompt。"""
        llm = _StubLLM(response_content='{"actions":[{"type":"idle","reason":"x"}]}')
        planner = LLMPlanner(llm_client=llm)
        names = planner._prompt_registry.section_names()
        self.assertIn("identity", names)
        self.assertNotIn("persona", names)

    def test_persona_card_scoped_to_send_message_usage_doc(self) -> None:
        """角色卡随 send_message 的 allowed_scopes=("group","private") 走 scope
        过滤：group 的 tools_usage 含小奏卡，system loop 的完全不含。"""
        from qqbot.services.agent_loop.tools import build_default_registry

        reg = build_default_registry()
        group_docs = reg.usage_docs("group")
        self.assertIn("## Tool: send_message", group_docs)
        self.assertIn("小奏", group_docs)
        system_docs = reg.usage_docs("system")
        self.assertNotIn("## Tool: send_message", system_docs)
        self.assertNotIn("小奏", system_docs)

    def test_group_chat_rules_skipped_in_system_scope(self) -> None:
        """参与规则段只对有聊天面的 scope 渲染；system loop 的 system prompt
        不含 group_chat_rules（render(scope="system") 时该段返回空串）。"""
        llm = _StubLLM(response_content='{"actions":[{"type":"idle","reason":"x"}]}')
        planner = LLMPlanner(llm_client=llm)
        rendered_group = planner._prompt_registry.render(scope="group")
        rendered_system = planner._prompt_registry.render(scope="system")
        self.assertIn("什么时候调用 send_message", rendered_group)
        self.assertNotIn("什么时候调用 send_message", rendered_system)
        # 协议与身份段两个 scope 都在
        self.assertIn("decision engine", rendered_system)
        self.assertIn("tasks persist, conversation flows around them", rendered_system)

    def test_note_task_progress_action_parsed(self) -> None:
        body = (
            '{"actions":[{"type":"note_task_progress",'
            '"task_id":"T1","note":"need to recheck the log"}]}'
        )
        llm = _StubLLM(response_content=body)
        planner = LLMPlanner(llm_client=llm)
        out = asyncio.run(planner.decide(_ctx()))
        self.assertEqual(len(out.actions), 1)
        note_action = out.actions[0]
        self.assertIsInstance(note_action, NoteTaskProgressAction)
        self.assertEqual(note_action.task_id, "T1")
        self.assertEqual(note_action.note, "need to recheck the log")

    def test_create_task_with_triggered_by_event_id(self) -> None:
        body = (
            '{"actions":[{"type":"create_task","description":"d",'
            '"triggered_by_event_id":"MSG_42"}]}'
        )
        llm = _StubLLM(response_content=body)
        planner = LLMPlanner(llm_client=llm)
        out = asyncio.run(planner.decide(_ctx()))
        ct = out.actions[0]
        self.assertIsInstance(ct, CreateTaskAction)
        self.assertEqual(ct.triggered_by_event_id, "MSG_42")

    def test_system_prompt_includes_xml_format_doc(self) -> None:
        """xml_format.md 必须注入 system prompt —— LLM 据此读懂 <agent-input>
        信封的标签语义。锚定文档头和几个关键概念即可，避免绑死文案。"""
        llm = _StubLLM(
            response_content='{"actions":[{"type":"idle","reason":"x"}]}'
        )
        planner = LLMPlanner(llm_client=llm)
        asyncio.run(planner.decide(_ctx()))
        content = llm.invocations[0][0].content

        # xml_format.md 文档头
        self.assertIn("reading the `<agent-input>` envelope", content)
        # 关键标签必须解释过
        self.assertIn("<tool-catalog>", content)
        self.assertIn("<active-tasks>", content)
        self.assertIn("<timeline>", content)
        # 2026-07-02 起不再有 pending-tool-results 区（工具结果只在 timeline
        # 单点呈现，防双重渲染诱发复读）——文档不得再教这个标签
        self.assertNotIn("<pending-tool-results>", content)
        # 特殊标记
        self.assertIn("<truncated/>", content)
        self.assertIn("<processing/>", content)
        # 两态语义必须教过：status 只有 processing / complete
        self.assertIn('status="complete"', content)
        self.assertIn('status="processing"', content)

    def test_system_prompt_includes_group_chat_rules_doc(self) -> None:
        """group_chat_rules.md 必须注入 system prompt —— 规划层据此判断
        "有没有一条消息构成发言理由"（规则判断，非人格判断）。"""
        llm = _StubLLM(
            response_content='{"actions":[{"type":"idle","reason":"x"}]}'
        )
        planner = LLMPlanner(llm_client=llm)
        asyncio.run(planner.decide(_ctx()))
        content = llm.invocations[0][0].content

        # 文档头
        self.assertIn("什么时候调用 send_message", content)
        # 关键锚点：正反两张理由清单 + addressee 判别 + idle 常态
        self.assertIn("构成发言理由", content)
        self.assertIn("不构成发言理由", content)
        self.assertIn("被提到不等于被叫到", content)
        self.assertIn("`idle` 是常态结果", content)
        # 行为约束里仍要涉及备选决策
        self.assertIn("note_task_progress", content)
        # 复读禁令（2026-07-01 批次语义）：已 complete 的 send_message = 已说
        # 出，绝不再发。protocol 与 group_chat_rules 两处都要教。
        self.assertIn("别复读自己", content)
        self.assertIn("One tick, one tool batch", content)

    def test_system_prompt_explicitly_forbids_bare_text_as_reply(self) -> None:
        """system prompt 必须明确：想让群里看到的话只能放进 send_message 工具，
        不能塞进 reasoning / task 字段假装已经回复。"""
        llm = _StubLLM(
            response_content='{"actions":[{"type":"idle","reason":"x"}]}'
        )
        planner = LLMPlanner(llm_client=llm)
        asyncio.run(planner.decide(_ctx()))
        content = llm.invocations[0][0].content

        self.assertIn(
            'it must live inside `call_tool(tool_name="send_message").arguments.content`',
            content,
        )
        self.assertIn("没调 `send_message` 就等于你还没说", content)

    def test_default_prompt_section_order(self) -> None:
        """五段必须按 order 升序拼接：
        identity < xml_format < group_chat_rules < protocol < tools_usage。
        LLM 按"你是什么→怎么读输入→什么时候需要发言→怎么决定→工具"递进读。
        人设不再是独立段（角色卡在 tools/send_message.md 里随 tools_usage 出现）。"""
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

        idx_identity = content.index("decision engine")
        idx_xml = content.index("reading the `<agent-input>` envelope")
        idx_protocol = content.index("tasks persist, conversation flows around them")
        idx_group = content.index("什么时候调用 send_message")
        idx_tools = content.index("STUB-TOOL-ORDER-MARKER")

        self.assertLess(idx_identity, idx_xml)
        self.assertLess(idx_xml, idx_group)
        self.assertLess(idx_group, idx_protocol)
        self.assertLess(idx_protocol, idx_tools)

    def test_reply_tool_usage_doc_renders_via_tool_registry(self) -> None:
        """SendMessageTool.usage_prompt（tools/send_message.md）必须随
        ToolRegistry.usage_docs 进 system prompt 的 tools_usage 段；send_message
        现在和 websearch / search_history 同构。"""
        from qqbot.services.agent_loop.tools import build_default_registry

        # 工具无构造依赖；usage_docs 只读 usage_prompt，不触发任何运行期依赖
        reg = build_default_registry()

        llm = _StubLLM(
            response_content='{"actions":[{"type":"idle","reason":"x"}]}'
        )
        planner = LLMPlanner(llm_client=llm, tool_registry=reg)
        asyncio.run(planner.decide(_ctx()))
        content = llm.invocations[0][0].content

        # 按工具名分段的标题
        self.assertIn("## Tool: send_message", content)
        # send_message.md 标志性段落
        self.assertIn("your one and only way to speak", content)
        # OneBot V11 段示例关键字面
        self.assertIn('"type": "at"', content)
        # @ 全体成员的 qq:"all" 约定
        self.assertIn('"all"', content)

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

        self.assertIn("## Tool: stub_tool", content)
        self.assertIn("STUB-TOOL-USAGE-MARKER", content)

    def test_system_prompt_skips_tool_without_usage_prompt(self) -> None:
        """没写 sibling .md 的工具不应在 system prompt 里产生孤儿
        `## Tool: foo` 空标题。"""
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

        self.assertNotIn("## Tool: no_usage_tool", content)

    def test_custom_prompt_registry_overrides_default(self) -> None:
        """传入自定义 PromptRegistry 时绕过默认装配 —— 调用方拥有最终拼接权。"""
        from qqbot.services.agent_loop.prompt_registry import PromptRegistry

        custom = PromptRegistry()
        custom.register("only-section", 0, "CUSTOM-ONLY-MARKER")

        llm = _StubLLM(
            response_content='{"actions":[{"type":"idle","reason":"x"}]}'
        )
        planner = LLMPlanner(
            llm_client=llm,
            prompt_registry=custom,
        )
        asyncio.run(planner.decide(_ctx()))
        content = llm.invocations[0][0].content

        self.assertEqual(content, "CUSTOM-ONLY-MARKER")

    def test_system_prompt_is_task_centric(self) -> None:
        """新协议要求 LLM 围绕 active_tasks 决策；这里只验证关键约束词出现，
        不绑定文案细节（避免无谓脆弱）。"""
        llm = _StubLLM(
            response_content='{"actions":[{"type":"idle","reason":"x"}]}'
        )
        planner = LLMPlanner(llm_client=llm)
        asyncio.run(planner.decide(_ctx()))
        content = llm.invocations[0][0].content

        # task 状态机词汇必须暴露给 LLM
        self.assertIn("active_tasks", content)
        self.assertIn("complete_task", content)
        self.assertIn("fail_task", content)
        # reasoning 必须以 active_tasks 为中心（把它当作 standing agenda 逐条评估）
        self.assertIn("standing agenda", content)
        # 必须明示"新消息不会自动取消 task"
        self.assertTrue(
            "do NOT cancel" in content or "does not implicitly close" in content,
            "prompt should explicitly state that new messages do not cancel tasks",
        )

    def test_multimodal_human_message_dedup_by_hash(self) -> None:
        """timeline 里同一 hash 出现多次 → 只附一份 image_url block；
        text block 永远是 list 中第一个，方便 LLM 找到主提示。"""
        import base64
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            p1 = Path(tmp) / "h1"
            p1.write_bytes(b"\x89PNG-bytes-1")
            p2 = Path(tmp) / "h2"
            p2.write_bytes(b"\x89PNG-bytes-2")

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
                        render='<message>hi <image hash="h1"/></message>',
                        images=[
                            ImageRef(
                                file_hash="h1",
                                local_path=str(p1),
                                mime="image/png",
                            )
                        ],
                    ),
                    TimelineItem(
                        event_id="E2",
                        occurred_at=china_now(),
                        kind="message",
                        render='<message><image hash="h1"/></message>',
                        # 同 hash 再次出现 — 不应再附 block
                        images=[
                            ImageRef(
                                file_hash="h1",
                                local_path=str(p1),
                                mime="image/png",
                            )
                        ],
                    ),
                    TimelineItem(
                        event_id="E3",
                        occurred_at=china_now(),
                        kind="message",
                        render='<message><image hash="h2"/></message>',
                        images=[
                            ImageRef(
                                file_hash="h2",
                                local_path=str(p2),
                                mime="image/jpeg",
                            )
                        ],
                    ),
                ],
            )

            llm = _StubLLM(
                response_content='{"actions":[{"type":"idle","reason":"x"}]}'
            )
            planner = LLMPlanner(llm_client=llm)
            asyncio.run(planner.decide(ctx))

        human_content = llm.invocations[0][1].content
        # content 必须是分块 list（不能再是纯字符串），首块是 text，其后是图
        self.assertIsInstance(human_content, list)
        self.assertEqual(human_content[0]["type"], "text")
        image_blocks = [b for b in human_content if b["type"] == "image_url"]
        self.assertEqual(len(image_blocks), 2)  # h1 去重，h2 各一
        urls = [b["image_url"]["url"] for b in image_blocks]
        b64_1 = base64.b64encode(b"\x89PNG-bytes-1").decode("ascii")
        b64_2 = base64.b64encode(b"\x89PNG-bytes-2").decode("ascii")
        self.assertIn(f"data:image/png;base64,{b64_1}", urls)
        self.assertIn(f"data:image/jpeg;base64,{b64_2}", urls)

    def test_multimodal_image_blocks_preceded_by_hash_label(self) -> None:
        """每个 image_url block 前面必须有一个文本 block，内容包含该图的 hash。
        这是 VLM 把 XML 里 `<image hash="X"/>` 占位符和实际像素绑定的桥梁——
        没有这个 label，3 张图以上模型就会按出现顺序错位（用户说"上上一张图"
        定位不到）。"""
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            p1 = Path(tmp) / "h1"
            p1.write_bytes(b"\x89PNG-bytes-1")
            p2 = Path(tmp) / "h2"
            p2.write_bytes(b"\x89PNG-bytes-2")

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
                        render='<message><image hash="hash-A"/></message>',
                        images=[
                            ImageRef(
                                file_hash="hash-A",
                                local_path=str(p1),
                                mime="image/png",
                            )
                        ],
                    ),
                    TimelineItem(
                        event_id="E2",
                        occurred_at=china_now(),
                        kind="message",
                        render='<message><image hash="hash-B"/></message>',
                        images=[
                            ImageRef(
                                file_hash="hash-B",
                                local_path=str(p2),
                                mime="image/jpeg",
                            )
                        ],
                    ),
                ],
            )

            llm = _StubLLM(
                response_content='{"actions":[{"type":"idle","reason":"x"}]}'
            )
            planner = LLMPlanner(llm_client=llm)
            asyncio.run(planner.decide(ctx))

        human_content = llm.invocations[0][1].content
        # 每张 image_url 的前一个 block 必须是 text 且包含对应 hash
        for i, b in enumerate(human_content):
            if b.get("type") != "image_url":
                continue
            prev = human_content[i - 1]
            self.assertEqual(prev["type"], "text")
            # label 应该提到这张图的 hash —— 用 hash-A 出现在某 label 文本里
            # 来锁定"label 紧挨 image"绑定关系
            self.assertTrue(
                "hash-A" in prev["text"] or "hash-B" in prev["text"],
                f"image at index {i} not preceded by a hash label: {prev!r}",
            )

    def test_multimodal_skips_missing_file(self) -> None:
        """落盘文件被清理 / 路径不存在 → 跳过该图，整 tick 仍然成功。
        text 里的 <image hash="..."/> 占位还在，LLM 知道图存在过。"""
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
                    render='<message><image hash="gone"/></message>',
                    images=[
                        ImageRef(
                            file_hash="gone",
                            local_path="/nonexistent/path/gone",
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
        self.assertIsInstance(human_content, list)
        self.assertEqual(
            [b for b in human_content if b["type"] == "image_url"], []
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
        human_text = llm.invocations[0][1].content[0]["text"]
        self.assertIn('bot_qq="3167291813"', human_text)
        # scope/now/tick 也仍在
        self.assertIn('scope="group:100"', human_text)
        self.assertIn('tick="1"', human_text)

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
        human_text = llm.invocations[0][1].content[0]["text"]
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
        human_text = llm.invocations[0][1].content[0]["text"]
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
        human_text = llm.invocations[0][1].content[0]["text"]
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
        human_text = llm.invocations[0][1].content[0]["text"]
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
        human_text = llm.invocations[0][1].content[0]["text"]
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
        # llm_client=None and create_llm() will probably return None too
        # (no LLM_API_KEY in test env), so this exercises that branch.
        # We simulate the empty path by stubbing _ensure_llm to return None.
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


class EnvelopeSelfMemoryTests(unittest.TestCase):
    """<validation-error>（2026-07-02）与 <last-reasoning> 移除（2026-07-06
    思考轨迹内联：自我记忆随投影层的 <my-thought> timeline 行进信封，
    llm_planner 不再渲染独立区块——见 test_agent_loop_projection_contract
    的 MyThoughtTests）。"""

    def _render_with(self, **overrides: Any) -> str:
        from dataclasses import replace

        llm = _StubLLM(
            response_content='{"actions":[{"type":"idle","reason":"x"}]}'
        )
        planner = LLMPlanner(llm_client=llm)
        ctx = replace(_ctx(), **overrides)
        asyncio.run(planner.decide(ctx))
        return llm.invocations[0][1].content[0]["text"]

    def test_last_reasoning_block_removed(self) -> None:
        # DecisionContext 已无 last_reasoning 字段，信封任何情况下都不得再
        # 出现 <last-reasoning> 区块（防复活：它会与 <my-thought> 双重渲染）。
        from qqbot.services.agent_loop.decision import DecisionContext

        xml = self._render_with()
        self.assertNotIn("<last-reasoning", xml)
        self.assertNotIn("<validation-error", xml)
        self.assertFalse(hasattr(_ctx(), "last_reasoning"))
        self.assertFalse(hasattr(DecisionContext, "last_reasoning"))

    def test_my_thought_timeline_rows_pass_through_envelope(self) -> None:
        # 思考行是普通 TimelineItem：planner 原样拼进 <timeline>，无需特殊处理
        from dataclasses import replace

        row = TimelineItem(
            event_id="D1",
            occurred_at=china_now(),
            kind="my_thought",
            render='<my-thought time="2026-07-06T12:00:00+08:00">先观望</my-thought>',
        )
        llm = _StubLLM(
            response_content='{"actions":[{"type":"idle","reason":"x"}]}'
        )
        planner = LLMPlanner(llm_client=llm)
        asyncio.run(planner.decide(replace(_ctx(), timeline=[row])))
        xml = llm.invocations[0][1].content[0]["text"]
        self.assertIn("<my-thought time=", xml)
        self.assertIn("先观望", xml)

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


class SavedMemesEnvelopeTests(unittest.TestCase):
    """<saved-memes> 渲染契约（表情包工具黑盒设计 §prompt 注入）：
    有收藏才渲染整段；每条 <meme> 带 hash / saved_at 属性 + 描述正文
    （XML 转义）；位置在 </active-tasks> 之后、<timeline> 之前。"""

    def _envelope_text(self, ctx: DecisionContext) -> str:
        llm = _StubLLM(
            response_content='{"actions":[{"type":"idle","reason":"x"}]}'
        )
        planner = LLMPlanner(llm_client=llm)
        asyncio.run(planner.decide(ctx))
        return llm.invocations[0][1].content[0]["text"]

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

    def test_saved_memes_rendered_between_tasks_and_timeline(self) -> None:
        text = self._envelope_text(
            self._ctx_with_memes("黑猫瞪眼，配字就这，嘲讽用")
        )
        self.assertIn("<saved-memes>", text)
        self.assertIn(f'<meme hash="{"ab" * 32}"', text)
        self.assertIn('saved_at="', text)
        self.assertIn("黑猫瞪眼，配字就这，嘲讽用", text)
        # 布局：静态参考 → 动态状态 → timeline 尾部（recency bias）
        self.assertLess(
            text.index("</active-tasks>"), text.index("<saved-memes>")
        )
        self.assertLess(
            text.index("</saved-memes>"), text.index("<timeline>")
        )

    def test_no_saved_memes_omits_section(self) -> None:
        # 空收藏整段省略——不渲染空 <saved-memes>。
        text = self._envelope_text(_ctx())
        self.assertNotIn("<saved-memes>", text)

    def test_description_xml_escaped(self) -> None:
        text = self._envelope_text(self._ctx_with_memes("A<B&C"))
        self.assertIn("A&lt;B&amp;C", text)
        self.assertNotIn(">A<B&C<", text)


if __name__ == "__main__":
    unittest.main()

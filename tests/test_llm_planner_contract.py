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
    ReplyAction,
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

    def test_reply_action_parsed(self) -> None:
        body = (
            '{"reasoning":"hi user","actions":['
            '{"type":"reply",'
            '"content":[{"type":"text","data":{"text":"hello"}}],'
            '"target":{"kind":"group","group_id":100},'
            '"related_msg_hashes":["h1"]}'
            "]}"
        )
        llm = _StubLLM(response_content=body)
        planner = LLMPlanner(llm_client=llm)
        out = asyncio.run(planner.decide(_ctx()))
        self.assertEqual(out.reasoning, "hi user")
        self.assertEqual(len(out.actions), 1)
        reply = out.actions[0]
        self.assertIsInstance(reply, ReplyAction)
        self.assertEqual(reply.target, {"kind": "group", "group_id": 100})
        self.assertEqual(reply.related_msg_hashes, ["h1"])

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

    def test_persona_text_prepended_to_system_prompt(self) -> None:
        llm = _StubLLM(response_content='{"actions":[{"type":"idle","reason":"x"}]}')
        planner = LLMPlanner(
            llm_client=llm,
            persona_text="你是小奏，傲娇但温柔。",
        )
        asyncio.run(planner.decide(_ctx()))
        # _build_messages 拼接结果应该出现在 SystemMessage.content 里
        self.assertEqual(len(llm.invocations), 1)
        messages = llm.invocations[0]
        system_msg = messages[0]
        self.assertIn("你是小奏，傲娇但温柔。", system_msg.content)
        # 协议部分仍然存在
        self.assertIn("autonomous QQ group bot agent", system_msg.content)
        # 分隔符确认人设在前
        self.assertLess(
            system_msg.content.index("你是小奏"),
            system_msg.content.index("autonomous QQ group bot agent"),
        )

    def test_persona_none_falls_back_to_plain_protocol_prompt(self) -> None:
        # persona=None：PromptRegistry 应当不注册 persona section，
        # 协议段仍在；reply_usage / tools_usage 之间的分隔符与 persona
        # 无关，因此不再断言 "no ---"。
        llm = _StubLLM(response_content='{"actions":[{"type":"idle","reason":"x"}]}')
        planner = LLMPlanner(llm_client=llm, persona_text=None)
        asyncio.run(planner.decide(_ctx()))
        system_msg = llm.invocations[0][0]
        self.assertIn("autonomous QQ group bot agent", system_msg.content)
        self.assertNotIn("persona", planner._prompt_registry.section_names())

    def test_persona_whitespace_only_treated_as_none(self) -> None:
        # 同上：纯空白 persona 视作未提供，PromptRegistry 不注册 persona 段
        llm = _StubLLM(response_content='{"actions":[{"type":"idle","reason":"x"}]}')
        planner = LLMPlanner(llm_client=llm, persona_text="   \n\t  ")
        asyncio.run(planner.decide(_ctx()))
        self.assertNotIn("persona", planner._prompt_registry.section_names())

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

    def test_system_prompt_includes_reply_usage_doc(self) -> None:
        """reply.md 内容必须出现在 SystemMessage 里 —— LLM 才能用 at /
        reply / face 这些 OneBot V11 段。锚两个特征关键词即可，文案细节
        不绑死。"""
        llm = _StubLLM(
            response_content='{"actions":[{"type":"idle","reason":"x"}]}'
        )
        planner = LLMPlanner(llm_client=llm)
        asyncio.run(planner.decide(_ctx()))
        content = llm.invocations[0][0].content

        # reply.md 文档头
        self.assertIn("Reply usage", content)
        # at 段示例的关键字面
        self.assertIn('"type": "at"', content)
        # reply 引用回复段示例的关键字面
        self.assertIn('"type": "reply"', content)
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
            persona_text="should-be-ignored",
            prompt_registry=custom,
        )
        asyncio.run(planner.decide(_ctx()))
        content = llm.invocations[0][0].content

        self.assertEqual(content, "CUSTOM-ONLY-MARKER")
        self.assertNotIn("should-be-ignored", content)

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
        # 必须有硬约束：reasoning 先评估 active task
        self.assertIn("MUST", content)
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


if __name__ == "__main__":
    unittest.main()

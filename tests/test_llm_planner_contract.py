"""Contracts for source-shaped LLM Planner output and envelope rendering."""

from __future__ import annotations

import unittest
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from typing import Any

from qqbot.core.time import CHINA_TIMEZONE
from qqbot.services.agent_loop.decision import (
    DecisionContext,
    ProgramValidationFeedback,
)
from qqbot.services.agent_loop.llm_planner import (
    LLMPlanner,
    _build_messages,
    _render_input_text,
    build_default_prompt_library,
)
from qqbot.services.agent_loop.tool_registry import BaseTool, ToolRegistry


def _ctx(**changes: Any) -> DecisionContext:
    values = {
        "scope_key": "group:42",
        "correlation_id": "corr-1",
        "tick_seq": 3,
        "now": datetime(2026, 8, 3, 12, 30, tzinfo=CHINA_TIMEZONE),
    }
    values.update(changes)
    return DecisionContext(**values)


class _PromptLibrary:
    def render_sections(self, *, scope: str):
        return [SimpleNamespace(name="root", text=f"system for {scope}")]


@dataclass
class _Response:
    content: Any


class _LLM:
    def __init__(self, response: Any = "# idle", error: Exception | None = None):
        self.response = response
        self.error = error
        self.calls: list[list[Any]] = []
        self.failed_reasons: list[str] = []

    async def ainvoke(self, messages: list[Any]) -> _Response:
        self.calls.append(messages)
        if self.error is not None:
            raise self.error
        return _Response(self.response)

    def mark_last_call_failed(self, reason: str) -> None:
        self.failed_reasons.append(reason)


class LLMPlannerDecisionTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_model_call_returns_source_verbatim(self) -> None:
        source = (
            "```python\nr = websearch(query='x')\n"
            "return {'n': len(r.results)}\n```"
        )
        llm = _LLM(source)
        planner = LLMPlanner(llm_client=llm, prompt_library=_PromptLibrary())
        output = await planner.decide(_ctx())
        self.assertEqual(output.program, source)
        self.assertEqual(output.raw_response, source)
        self.assertIsNone(output.planner_error)
        self.assertEqual(len(llm.calls), 1)

    async def test_transport_failure_degrades_to_empty_program(self) -> None:
        llm = _LLM(error=RuntimeError("offline"))
        planner = LLMPlanner(llm_client=llm, prompt_library=_PromptLibrary())
        output = await planner.decide(_ctx())
        self.assertEqual(output.program, "")
        self.assertEqual(output.planner_error, "llm_call_error:RuntimeError")
        self.assertEqual(len(llm.calls), 1)

    async def test_unavailable_client_degrades_to_empty_program(self) -> None:
        class _Unavailable(LLMPlanner):
            async def _ensure_llm(self) -> Any:
                return None

        planner = _Unavailable(prompt_library=_PromptLibrary())
        output = await planner.decide(_ctx())
        self.assertEqual(output.program, "")
        self.assertEqual(output.planner_error, "llm_unavailable")

    async def test_invalid_output_report_is_thin_route_forwarder(self) -> None:
        llm = _LLM()
        planner = LLMPlanner(llm_client=llm, prompt_library=_PromptLibrary())
        planner.report_invalid_output("program_syntax_error:bad indent")
        self.assertEqual(
            llm.failed_reasons,
            ["program_syntax_error:bad indent"],
        )


class PlannerEnvelopeTests(unittest.TestCase):
    def test_human_envelope_has_no_tool_catalog(self) -> None:
        messages, _ = _build_messages(_ctx(), _PromptLibrary())
        human = str(messages[1].content)
        self.assertIn("## 时间线", human)
        self.assertNotIn("## 工具目录", human)
        self.assertNotIn("arguments_schema", human)

    def test_production_context_has_no_validation_feedback_block(self) -> None:
        """2026-08-11：同拍不再回灌校验拒绝；生产 context 尾部只有 <现在>。"""
        rendered = _render_input_text(_ctx())
        self.assertIn("<现在>", rendered)
        self.assertNotIn("<校验拒绝>", rendered)
        self.assertNotIn("<rejected-program>", rendered)

    def test_legacy_validation_feedback_still_renders_if_injected(self) -> None:
        """字段未删：测试/旧快照若注入 feedback，渲染器仍可转义输出。"""
        feedback = ProgramValidationFeedback(
            attempt=1,
            error_kind="program_forbidden_construct",
            message="method calls are forbidden",
            rejected_program='names = "、".join(items)\nreturn {"x": names}',
            line=1,
            column=8,
        )
        rendered = _render_input_text(_ctx(validation_feedback=feedback))
        self.assertIn("<校验拒绝>", rendered)
        self.assertIn("  <rejected-program>", rendered)

    def test_program_api_reference_lives_in_system_prompt(self) -> None:
        class _Query(BaseTool):
            name = "lookup"
            program_kind = "query"
            description = "lookup data"
            arguments_schema = {"type": "object", "properties": {}}
            result_schema = {
                "type": "object",
                "properties": {"value": {"type": "string"}},
            }

            async def execute(self, arguments: dict, **context: Any):
                return {"value": "x"}

        registry = ToolRegistry()
        registry.register(_Query)
        prompt = build_default_prompt_library(tool_registry=registry).render(
            scope="group"
        )
        self.assertIn("## 程序函数：lookup", prompt)
        self.assertIn("返回 schema", prompt)
        self.assertIn("响应正文就是一段受限 Python 源码", prompt)


if __name__ == "__main__":
    unittest.main()

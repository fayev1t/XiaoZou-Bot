"""AgentLoop contracts for program-shaped decisions (endpoint failover, no rewrite)."""

# Async mocks accept the production call shape while recording only ordering.
# ruff: noqa: ARG001

from __future__ import annotations

import hashlib
import unittest
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from qqbot.services.agent_loop.decision import DecisionContext, DecisionOutput
from qqbot.services.agent_loop.loop import AgentLoop
from qqbot.services.agent_loop.tool_registry import ToolRegistry

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


class _SequencePlanner:
    def __init__(self, *programs: str) -> None:
        self._programs = list(programs)
        self.contexts: list[DecisionContext] = []
        self.reports: list[str] = []

    async def decide(self, context: DecisionContext) -> DecisionOutput:
        self.contexts.append(context)
        return DecisionOutput(program=self._programs.pop(0))

    def report_invalid_output(self, reason: str) -> None:
        self.reports.append(reason)


def _context() -> DecisionContext:
    return DecisionContext(
        scope_key="group:1",
        correlation_id="CORR",
        tick_seq=1,
        now=NOW,
    )


class ProgramPreflightFailoverContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_static_error_reports_and_retries_without_validation_feedback(
        self,
    ) -> None:
        """preflight 失败：冷却端点 + 再 decide，context 不带校验拒绝。"""
        planner = _SequencePlanner("import os", "# repaired")
        loop = AgentLoop(
            scope_key="group:1",
            planner=planner,
            session_factory=object(),
            tool_registry=ToolRegistry(),
        )
        with patch(
            "qqbot.services.agent_loop.loop.write_runtime_event",
            new=AsyncMock(return_value="INVALID_EVENT"),
        ) as write_runtime:
            decision, prepared, error = await loop._decide_program(_context())

        self.assertIsNotNone(decision)
        self.assertIsNotNone(prepared)
        self.assertIsNone(error)
        self.assertEqual(prepared.source, "# repaired")
        self.assertEqual(len(planner.contexts), 2)
        self.assertEqual(len(planner.reports), 1)
        self.assertIn("program_forbidden_construct", planner.reports[0])
        for ctx in planner.contexts:
            self.assertIsNone(ctx.validation_feedback)
        write_runtime.assert_awaited_once()
        self.assertEqual(
            write_runtime.await_args.kwargs["event_type"],
            "runtime.llm_invalid_output",
        )
        self.assertEqual(write_runtime.await_args.kwargs["payload"]["attempt"], 1)

    async def test_each_static_failure_is_reported_before_next_attempt(self) -> None:
        order: list[str] = []

        class _OrderedPlanner(_SequencePlanner):
            async def decide(self, context: DecisionContext) -> DecisionOutput:
                order.append("decide")
                return await super().decide(context)

            def report_invalid_output(self, reason: str) -> None:
                order.append("report")
                super().report_invalid_output(reason)

        async def _write_runtime(*args: Any, **kwargs: Any) -> str:
            order.append("invalid_event")
            return "E"

        planner = _OrderedPlanner("while True:\n    pass", "# fixed")
        loop = AgentLoop(
            scope_key="group:1",
            planner=planner,
            session_factory=object(),
            tool_registry=ToolRegistry(),
        )
        with patch(
            "qqbot.services.agent_loop.loop.write_runtime_event",
            new=_write_runtime,
        ):
            await loop._decide_program(_context())
        self.assertEqual(order, ["decide", "report", "invalid_event", "decide"])


class ProgramDecisionEventContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_three_static_failures_still_write_decision_root_and_terminal(
        self,
    ) -> None:
        rejected = ["import os", "while True:\n    pass", "raise ValueError()"]
        planner = _SequencePlanner(*rejected)
        loop = AgentLoop(
            scope_key="group:1",
            planner=planner,
            session_factory=object(),
            tool_registry=ToolRegistry(),
        )
        loop._recovery_done = True

        async def _runtime_event(*args: Any, **kwargs: Any) -> str:
            return (
                "TICK_STARTED"
                if kwargs["event_type"] == "runtime.tick_started"
                else "RUNTIME_EVENT"
            )

        runtime_mock = AsyncMock(side_effect=_runtime_event)
        with (
            patch(
                "qqbot.services.agent_loop.loop.write_runtime_event",
                new=runtime_mock,
            ),
            patch(
                "qqbot.services.agent_loop.loop.write_agent_event",
                new=AsyncMock(return_value="DECISION_ID"),
            ) as write_decision,
            patch(
                "qqbot.services.agent_loop.loop.write_program_failed",
                new=AsyncMock(return_value="PROGRAM_FAILED"),
            ) as write_failed,
        ):
            await loop._tick()

        self.assertEqual(len(planner.contexts), 3)
        self.assertEqual(len(planner.reports), 3)
        for ctx in planner.contexts:
            self.assertIsNone(ctx.validation_feedback)
        write_decision.assert_awaited_once()
        payload = write_decision.await_args.kwargs["payload"]
        self.assertEqual(payload["program"], rejected[-1])
        self.assertEqual(
            payload["program_sha256"],
            hashlib.sha256(rejected[-1].encode("utf-8")).hexdigest(),
        )
        write_failed.assert_awaited_once()
        failed_kwargs = write_failed.await_args.kwargs
        self.assertEqual(failed_kwargs["decision_id"], "DECISION_ID")
        self.assertEqual(failed_kwargs["error_kind"], "invalid_program_giveup")
        self.assertEqual(
            failed_kwargs["rejected_error_kind"],
            "program_forbidden_construct",
        )
        invalid_calls = [
            call
            for call in runtime_mock.await_args_list
            if call.kwargs.get("event_type") == "runtime.llm_invalid_output"
        ]
        self.assertEqual(len(invalid_calls), 3)

    async def test_runtime_failure_does_not_trigger_static_retry(self) -> None:
        planner = _SequencePlanner("# valid")
        loop = AgentLoop(
            scope_key="group:1",
            planner=planner,
            session_factory=object(),
            tool_registry=ToolRegistry(),
        )
        loop._recovery_done = True
        with (
            patch(
                "qqbot.services.agent_loop.loop.write_runtime_event",
                new=AsyncMock(return_value="RUNTIME"),
            ),
            patch(
                "qqbot.services.agent_loop.loop.write_agent_event",
                new=AsyncMock(return_value="DECISION"),
            ),
            patch.object(
                loop,
                "_execute_program",
                new=AsyncMock(return_value="failed"),
            ) as execute,
        ):
            await loop._tick()
        self.assertEqual(len(planner.contexts), 1)
        self.assertEqual(planner.reports, [])
        execute.assert_awaited_once()

    async def test_empty_program_has_decision_and_program_terminal_but_no_idle_event(
        self,
    ) -> None:
        planner = _SequencePlanner("# intentionally idle")
        loop = AgentLoop(
            scope_key="group:1",
            planner=planner,
            session_factory=object(),
            tool_registry=ToolRegistry(),
        )
        loop._recovery_done = True
        with (
            patch(
                "qqbot.services.agent_loop.loop.write_runtime_event",
                new=AsyncMock(return_value="RUNTIME"),
            ),
            patch(
                "qqbot.services.agent_loop.loop.write_agent_event",
                new=AsyncMock(return_value="DECISION"),
            ) as write_decision,
            patch(
                "qqbot.services.agent_loop.loop.write_program_completed",
                new=AsyncMock(return_value="PROGRAM_COMPLETED"),
            ) as write_completed,
        ):
            await loop._tick()

        write_decision.assert_awaited_once()
        write_completed.assert_awaited_once()
        completed = write_completed.await_args.kwargs
        self.assertEqual(completed["decision_id"], "DECISION")
        self.assertEqual(completed["query_calls"], [])
        self.assertEqual(completed["effect_call_ids"], [])
        self.assertFalse(completed["has_result"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

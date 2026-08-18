"""AgentLoop contracts for program-shaped decisions.

两组：端点切换（preflight 失败换模型、不回灌校验拒绝），以及 2026-08-17 的
提案-裁决流水线——写下的程序当拍只落库，要等后来某一拍
``execute_decision(event_id=…)`` 指名才交给 Runner 执行。
"""

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
from qqbot.services.agent_loop.program_events import ReferencedDecision
from qqbot.services.agent_loop.tool_registry import (
    BaseTool,
    ToolOutcome,
    ToolRegistry,
)

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
TARGET_ID = "01K2X9F3MQ8B4NVYRTC7HDZ6EW"


class _NotifyTool(BaseTool):
    """派发路径的替身；本文件的用例都在 `_tick` 层打桩，它不会真的被执行。"""

    name = "notify"
    program_kind = "effect"
    allowed_scopes = ("group",)
    arguments_schema = {
        "type": "object",
        "properties": {"message": {"type": "string"}},
        "required": ["message"],
        "additionalProperties": False,
    }
    result_schema = {
        "type": "object",
        "properties": {"sent": {"type": "boolean"}},
        "additionalProperties": False,
    }

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        return ToolOutcome.success({"sent": True})


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(_NotifyTool)
    return registry


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
        """静态重试只由 preflight 失败触发。

        2026-08-14 派发拍之后这条更强了：执行整个发生在 Runner 里、`_tick`
        之外，运行时失败**结构上**不可能回到 decide。2026-08-17 起更强一层：
        提案拍连派发都不做，只落库。
        """
        planner = _SequencePlanner("return 1")
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
            patch.object(loop._runner, "enqueue") as enqueue,
        ):
            await loop._tick()
        self.assertEqual(len(planner.contexts), 1)
        self.assertEqual(planner.reports, [])
        enqueue.assert_not_called()

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


class ProposalCommitPipelineContractTests(unittest.IsolatedAsyncioTestCase):
    """提案-裁决流水线（2026-08-17）。

    钉两件事。其一是唯一的不变量：**任何有副作用的程序都不可能在模型只看过一次
    世界的情况下跑起来**——写下它的那一拍只落库，让它生效的必须是后来重新读完
    时间线的另一拍。其二是两个层级互不相干：裁决作用在别的事件上，提案是本拍新
    写的代码，一次输出里可以两者都有。
    """

    def _loop(self, *programs: str) -> tuple[AgentLoop, _SequencePlanner]:
        planner = _SequencePlanner(*programs)
        loop = AgentLoop(
            scope_key="group:1",
            planner=planner,
            session_factory=object(),
            tool_registry=_registry(),
        )
        loop._recovery_done = True
        return loop, planner

    async def test_proposal_tick_persists_source_without_dispatching(self) -> None:
        loop, _ = self._loop('notify(message="hi")')
        statuses: list[str] = []

        async def _runtime(*args: Any, **kwargs: Any) -> str:
            if kwargs["event_type"] == "runtime.tick_ended":
                statuses.append(kwargs["payload"]["program_status"])
            return "RUNTIME"

        with (
            patch(
                "qqbot.services.agent_loop.loop.write_runtime_event",
                new=AsyncMock(side_effect=_runtime),
            ),
            patch(
                "qqbot.services.agent_loop.loop.write_agent_event",
                new=AsyncMock(return_value="DECISION"),
            ) as write_decision,
            patch(
                "qqbot.services.agent_loop.loop.write_program_completed",
                new=AsyncMock(),
            ) as write_completed,
            patch(
                "qqbot.services.agent_loop.loop.write_program_failed",
                new=AsyncMock(),
            ) as write_failed,
            patch.object(loop._runner, "enqueue") as enqueue,
            patch.object(loop, "_wake_continuation", return_value=True) as wake,
        ):
            await loop._tick()

        write_decision.assert_awaited_once()
        self.assertEqual(
            write_decision.await_args.kwargs["payload"]["program"],
            'notify(message="hi")',
        )
        # 一个函数都没跑：没入队，也没有任何终态。
        enqueue.assert_not_called()
        write_completed.assert_not_awaited()
        write_failed.assert_not_awaited()
        # 提案拍自己开下一拍去复核。
        wake.assert_called_once()
        self.assertEqual(statuses, ["proposed"])

    async def test_commit_tick_dispatches_the_referenced_program(self) -> None:
        loop, _ = self._loop(f'execute_decision(event_id="{TARGET_ID}")')
        statuses: list[str] = []

        async def _runtime(*args: Any, **kwargs: Any) -> str:
            if kwargs["event_type"] == "runtime.tick_ended":
                statuses.append(kwargs["payload"]["program_status"])
            return "RUNTIME"

        referenced = ReferencedDecision(
            event_id=TARGET_ID,
            correlation_id="OLD_CORR",
            program='notify(message="hi")',
            program_sha256="SHA",
        )
        with (
            patch(
                "qqbot.services.agent_loop.loop.write_runtime_event",
                new=AsyncMock(side_effect=_runtime),
            ),
            patch(
                "qqbot.services.agent_loop.loop.write_agent_event",
                new=AsyncMock(return_value="COMMIT_DECISION"),
            ) as write_decision,
            patch(
                "qqbot.services.agent_loop.loop.load_referenced_decision",
                new=AsyncMock(return_value=(referenced, None)),
            ) as load,
            patch(
                "qqbot.services.agent_loop.loop.write_program_failed",
                new=AsyncMock(),
            ) as write_failed,
            patch.object(loop._runner, "enqueue") as enqueue,
            patch.object(loop, "_wake_continuation", return_value=True) as wake,
        ):
            await loop._tick()

        load.assert_awaited_once()
        self.assertEqual(load.await_args.kwargs["event_id"], TARGET_ID)
        # 落库解耦：纯裁决剥完是空串。指令再嵌进 payload.program 就是套娃。
        stored = write_decision.await_args.kwargs["payload"]["program"]
        self.assertEqual(stored, "")
        self.assertNotIn("execute_decision", stored)
        enqueue.assert_called_once()
        queued = enqueue.call_args.args[0]
        # 跑的是被引用那条决策的源码，终态也挂回它自己的事件 ID 上——
        # "执行过没有"因此是一次对事件流的查询。
        self.assertEqual(queued.decision_id, TARGET_ID)
        self.assertEqual(queued.prepared.source, 'notify(message="hi")')
        # 程序的事件归属它的出处那一拍，不归属按下执行键的这一拍。
        self.assertEqual(queued.correlation_id, "OLD_CORR")
        write_failed.assert_not_awaited()
        # 唤醒交给被执行程序的 terminal 接力，本拍不自唤醒。
        wake.assert_not_called()
        self.assertEqual(statuses, ["dispatched"])

    async def test_commit_failure_lands_on_this_tick_and_wakes(self) -> None:
        """裁决报错按提案 §1.1 写 ``agent.program_failed``，挂在本拍决策上。"""
        loop, _ = self._loop(f'execute_decision(event_id="{TARGET_ID}")')
        statuses: list[str] = []

        async def _runtime(*args: Any, **kwargs: Any) -> str:
            if kwargs["event_type"] == "runtime.tick_ended":
                statuses.append(kwargs["payload"]["program_status"])
            return "RUNTIME"

        with (
            patch(
                "qqbot.services.agent_loop.loop.write_runtime_event",
                new=AsyncMock(side_effect=_runtime),
            ),
            patch(
                "qqbot.services.agent_loop.loop.write_agent_event",
                new=AsyncMock(return_value="COMMIT_DECISION"),
            ),
            patch(
                "qqbot.services.agent_loop.loop.load_referenced_decision",
                new=AsyncMock(return_value=(None, "already_executed")),
            ),
            patch(
                "qqbot.services.agent_loop.loop.write_program_failed",
                new=AsyncMock(return_value="FAILED"),
            ) as write_failed,
            patch.object(loop._runner, "enqueue") as enqueue,
            patch.object(loop, "_wake_continuation", return_value=True) as wake,
        ):
            await loop._tick()

        enqueue.assert_not_called()
        write_failed.assert_awaited_once()
        failed = write_failed.await_args.kwargs
        self.assertEqual(failed["decision_id"], "COMMIT_DECISION")
        self.assertEqual(failed["error_kind"], "already_executed")
        self.assertEqual(failed["target_event_id"], TARGET_ID)
        # 报错拍没有后台任务接力，必须自己开下一拍让模型看见报错。
        wake.assert_called_once()
        self.assertEqual(statuses, ["commit_rejected"])

    async def test_pipeline_tick_dispatches_and_persists_stripped_body(self) -> None:
        """④ 流水线混合：派发历史事件的同时把**剥离指令后**的新代码落库。

        落库解耦（§1.1 防套娃）：`payload.program` 里绝不能再嵌一条
        `execute_decision`，否则那条决策日后被指名时会再调度一次。
        """
        loop, _ = self._loop(
            f'execute_decision(event_id="{TARGET_ID}")\nnotify(message="next")'
        )
        runtime_events: list[dict] = []

        async def _runtime(*args: Any, **kwargs: Any) -> str:
            runtime_events.append(kwargs)
            return "RUNTIME"

        referenced = ReferencedDecision(
            event_id=TARGET_ID,
            correlation_id="OLD_CORR",
            program='notify(message="earlier")',
            program_sha256="SHA",
        )
        with (
            patch(
                "qqbot.services.agent_loop.loop.write_runtime_event",
                new=AsyncMock(side_effect=_runtime),
            ),
            patch(
                "qqbot.services.agent_loop.loop.write_agent_event",
                new=AsyncMock(return_value="MIXED_DECISION"),
            ) as write_decision,
            patch(
                "qqbot.services.agent_loop.loop.load_referenced_decision",
                new=AsyncMock(return_value=(referenced, None)),
            ),
            patch(
                "qqbot.services.agent_loop.loop.write_program_completed",
                new=AsyncMock(),
            ) as write_completed,
            patch.object(loop._runner, "enqueue") as enqueue,
            patch.object(loop, "_wake_continuation", return_value=True) as wake,
        ):
            await loop._tick()

        # 上层：派发被引用的那条，跑的是它自己的源码。
        enqueue.assert_called_once()
        queued = enqueue.call_args.args[0]
        self.assertEqual(queued.decision_id, TARGET_ID)
        self.assertEqual(queued.prepared.source, 'notify(message="earlier")')
        # 动作层：只有剥掉指令的纯业务代码落库，等以后被指名；此刻没有终态。
        stored = write_decision.await_args.kwargs["payload"]["program"]
        self.assertEqual(stored, 'notify(message="next")')
        self.assertNotIn("execute_decision", stored)
        write_completed.assert_not_awaited()
        # 恰好唤醒一次：交给被执行程序的 terminal，本拍不自唤醒。
        wake.assert_not_called()
        ended = [
            call
            for call in runtime_events
            if call["event_type"] == "runtime.tick_ended"
        ]
        self.assertEqual(ended[0]["payload"]["program_status"], "dispatched")
        self.assertTrue(ended[0]["payload"]["left_proposal"])

    async def test_naming_a_decision_with_an_empty_body_is_rejected(self) -> None:
        """判据是"那条决策的动作层有没有代码"。

        纯裁决拍也写 decision_emitted、也带 ev: 上屏（它的 program 被剥成空串），
        模型抄错一行就会指到它——没有东西可跑。
        """
        loop, _ = self._loop(f'execute_decision(event_id="{TARGET_ID}")')
        referenced = ReferencedDecision(
            event_id=TARGET_ID,
            correlation_id="OLD_CORR",
            program="",
            program_sha256="SHA",
        )
        with (
            patch(
                "qqbot.services.agent_loop.loop.write_runtime_event",
                new=AsyncMock(return_value="RUNTIME"),
            ),
            patch(
                "qqbot.services.agent_loop.loop.write_agent_event",
                new=AsyncMock(return_value="COMMIT_DECISION"),
            ),
            patch(
                "qqbot.services.agent_loop.loop.load_referenced_decision",
                new=AsyncMock(return_value=(referenced, None)),
            ),
            patch(
                "qqbot.services.agent_loop.loop.write_program_failed",
                new=AsyncMock(return_value="FAILED"),
            ) as write_failed,
            patch.object(loop._runner, "enqueue") as enqueue,
            patch.object(loop, "_wake_continuation", return_value=True),
        ):
            await loop._tick()

        enqueue.assert_not_called()
        self.assertEqual(
            write_failed.await_args.kwargs["error_kind"], "decision_not_a_proposal"
        )

    async def test_empty_program_closes_in_tick_and_never_wakes(self) -> None:
        """空程序是唯一的停止符：当拍收口、不唤醒，这段连续运行就结束。"""
        loop, _ = self._loop("# nothing to do")
        with (
            patch(
                "qqbot.services.agent_loop.loop.write_runtime_event",
                new=AsyncMock(return_value="RUNTIME"),
            ),
            patch(
                "qqbot.services.agent_loop.loop.write_agent_event",
                new=AsyncMock(return_value="DECISION"),
            ),
            patch(
                "qqbot.services.agent_loop.loop.write_program_completed",
                new=AsyncMock(return_value="COMPLETED"),
            ) as write_completed,
            patch.object(loop._runner, "enqueue") as enqueue,
            patch.object(loop, "_wake_continuation", return_value=True) as wake,
        ):
            await loop._tick()

        enqueue.assert_not_called()
        write_completed.assert_awaited_once()
        wake.assert_not_called()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

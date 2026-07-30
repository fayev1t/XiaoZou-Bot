"""Contract tests for AgentLoop action translation and inline task dispatch.

Planner actions are only ``idle`` / ``call_tool``. Task lifecycle operations
are normal calls to the ``task`` tool, whose ``execution_mode="inline"`` makes
AgentLoop await them in the current tick and atomically persist:

    agent.tool_called -> agent.task_* -> agent.tool_result/failed

Worker tools keep the existing dispatch path and task pending->running rule.
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from qqbot.services.agent_loop import (
    AgentLoop,
    CallToolAction,
    DecisionContext,
    DecisionOutput,
    IdleAction,
)
from qqbot.services.agent_loop.tool_registry import ToolRegistry
from qqbot.services.agent_loop.tools.task import TaskTool


class _RecordingSession:
    def __init__(self, store: list[Any]) -> None:
        self._store = store

    async def execute(self, stmt: Any, params: dict | None = None) -> Any:
        _ = params
        self._store.append(stmt)
        return SimpleNamespace(rowcount=1)

    async def commit(self) -> None:
        return None

    async def __aenter__(self) -> "_RecordingSession":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None


def _factory_for(store: list[Any]):
    def factory() -> _RecordingSession:
        return _RecordingSession(store)

    return factory


def _values(stmt: Any) -> dict:
    return {k: v for k, v in stmt.compile().params.items()}


def _is_event_stmt(stmt: Any) -> bool:
    table = getattr(stmt, "table", None)
    return table is not None and getattr(table, "name", None) == "agent_events"


def _types_after_tick_started(captured: list[Any]) -> list[str]:
    types = [_values(stmt).get("type") for stmt in captured]
    return [
        event_type
        for event_type in types
        if event_type not in ("runtime.tick_started", "runtime.tick_ended")
    ]


def _task_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(TaskTool())
    return registry


class _StaticPlanner:
    def __init__(self, output: DecisionOutput) -> None:
        self._output = output

    async def decide(self, context: DecisionContext) -> DecisionOutput:
        _ = context
        return self._output


async def _run_one_tick(planner: Any, scope_key: str) -> list[Any]:
    captured: list[Any] = []
    loop = AgentLoop(
        scope_key=scope_key,
        planner=planner,
        session_factory=_factory_for(captured),
        tool_registry=_task_registry(),
    )
    # Batch closing has its own DB-scripted contract tests. These action tests
    # focus on the event chain produced before the close query.
    with patch(
        "qqbot.services.agent_loop.loop.maybe_close_tool_batch",
        new=AsyncMock(return_value=False),
    ):
        loop.start()
        loop.wake(immediate=True)
        for _ in range(100):
            await asyncio.sleep(0.01)
            if any(
                _is_event_stmt(stmt)
                and _values(stmt).get("type") == "runtime.tick_ended"
                for stmt in captured
            ):
                break
        await loop.stop()
    return [stmt for stmt in captured if _is_event_stmt(stmt)]


class IdleActionTranslationTests(unittest.IsolatedAsyncioTestCase):
    async def test_idle_writes_agent_idle_decision(self) -> None:
        out = DecisionOutput(actions=[IdleAction(reason="nothing-to-do")])
        captured = await _run_one_tick(_StaticPlanner(out), "group:1")
        idle = next(
            stmt
            for stmt in captured
            if _values(stmt).get("type") == "agent.idle_decision"
        )
        self.assertEqual(_values(idle)["payload"]["reason"], "nothing-to-do")


class InlineTaskToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_writes_normal_tool_chain_and_task_event(self) -> None:
        out = DecisionOutput(
            actions=[
                CallToolAction(
                    tool_name="task",
                    arguments={
                        "action": "create",
                        "description": "check weather",
                        "related_tools": ["websearch"],
                        "task_ref": "T1",
                    },
                )
            ]
        )
        captured = await _run_one_tick(_StaticPlanner(out), "group:1")
        types = _types_after_tick_started(captured)
        self.assertEqual(
            types,
            [
                "agent.decision_emitted",
                "agent.tool_called",
                "agent.task_created",
                "agent.tool_result",
            ],
        )

        called = next(
            _values(stmt)
            for stmt in captured
            if _values(stmt).get("type") == "agent.tool_called"
        )
        created = next(
            _values(stmt)
            for stmt in captured
            if _values(stmt).get("type") == "agent.task_created"
        )
        terminal = next(
            _values(stmt)
            for stmt in captured
            if _values(stmt).get("type") == "agent.tool_result"
        )
        task_id = created["payload"]["task_id"]
        self.assertEqual(called["payload"]["tool_name"], "task")
        self.assertIsNone(called["payload"]["task_id"])
        self.assertEqual(created["causation_id"], called["event_id"])
        self.assertEqual(terminal["causation_id"], called["event_id"])
        self.assertEqual(terminal["payload"]["result"]["task_id"], task_id)
        self.assertEqual(terminal["payload"]["result"]["task_ref"], "T1")

    async def test_create_result_resolves_later_top_level_task_ref(self) -> None:
        out = DecisionOutput(
            actions=[
                CallToolAction(
                    tool_name="task",
                    arguments={
                        "action": "create",
                        "description": "x",
                        "task_ref": "r1",
                    },
                ),
                CallToolAction(
                    tool_name="websearch",
                    arguments={"query": "x"},
                    task_ref="r1",
                ),
            ]
        )
        captured = await _run_one_tick(_StaticPlanner(out), "group:1")
        created = next(
            _values(stmt)["payload"]
            for stmt in captured
            if _values(stmt).get("type") == "agent.task_created"
        )
        calls = [
            _values(stmt)["payload"]
            for stmt in captured
            if _values(stmt).get("type") == "agent.tool_called"
        ]
        web_call = next(call for call in calls if call["tool_name"] == "websearch")
        state_change = next(
            _values(stmt)["payload"]
            for stmt in captured
            if _values(stmt).get("type") == "agent.task_state_changed"
        )
        self.assertEqual(web_call["task_id"], created["task_id"])
        self.assertEqual(state_change["task_id"], created["task_id"])
        self.assertEqual(state_change["to_state"], "running")

    async def test_note_complete_and_fail_emit_task_domain_events(self) -> None:
        cases = [
            (
                {"action": "note", "task_id": "T1", "note": "found it"},
                "agent.task_progress_noted",
                {"task_id": "T1", "note": "found it"},
            ),
            (
                {
                    "action": "complete",
                    "task_id": "T1",
                    "result_summary": "done",
                },
                "agent.task_state_changed",
                {"task_id": "T1", "to_state": "done", "reason": "done"},
            ),
            (
                {"action": "fail", "task_id": "T1", "reason": "blocked"},
                "agent.task_state_changed",
                {"task_id": "T1", "to_state": "failed", "reason": "blocked"},
            ),
        ]
        for arguments, event_type, expected in cases:
            with self.subTest(action=arguments["action"]):
                out = DecisionOutput(
                    actions=[CallToolAction(tool_name="task", arguments=arguments)]
                )
                captured = await _run_one_tick(_StaticPlanner(out), "group:1")
                domain = next(
                    _values(stmt)["payload"]
                    for stmt in captured
                    if _values(stmt).get("type") == event_type
                )
                for key, value in expected.items():
                    self.assertEqual(domain[key], value)
                self.assertIn(
                    "agent.tool_result", _types_after_tick_started(captured)
                )

    async def test_create_uses_normal_triggered_by_event_id_context(self) -> None:
        out = DecisionOutput(
            actions=[
                CallToolAction(
                    tool_name="task",
                    arguments={"action": "create", "description": "d"},
                    triggered_by_event_id="MSG_99",
                )
            ]
        )
        captured = await _run_one_tick(_StaticPlanner(out), "group:1")
        created = next(
            _values(stmt)["payload"]
            for stmt in captured
            if _values(stmt).get("type") == "agent.task_created"
        )
        self.assertEqual(created["triggered_by_event_id"], "MSG_99")

    async def test_invalid_task_arguments_end_as_tool_failed(self) -> None:
        out = DecisionOutput(
            actions=[
                CallToolAction(
                    tool_name="task",
                    arguments={"action": "create", "description": ""},
                )
            ]
        )
        captured = await _run_one_tick(_StaticPlanner(out), "group:1")
        types = _types_after_tick_started(captured)
        self.assertIn("agent.tool_failed", types)
        self.assertNotIn("agent.task_created", types)
        failed = next(
            _values(stmt)["payload"]
            for stmt in captured
            if _values(stmt).get("type") == "agent.tool_failed"
        )
        self.assertEqual(failed["error_kind"], "invalid_arguments")


class WorkerToolDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_tool_writes_called_without_terminal(self) -> None:
        out = DecisionOutput(
            actions=[
                CallToolAction(
                    tool_name="websearch",
                    arguments={"query": "x"},
                )
            ]
        )
        captured = await _run_one_tick(_StaticPlanner(out), "group:1")
        types = _types_after_tick_started(captured)
        self.assertIn("agent.tool_called", types)
        self.assertNotIn("agent.tool_result", types)
        self.assertNotIn("agent.tool_failed", types)

    async def test_worker_call_with_task_id_auto_advances_state(self) -> None:
        out = DecisionOutput(
            actions=[
                CallToolAction(
                    tool_name="websearch",
                    arguments={"query": "x"},
                    task_id="EXTERNAL_TASK_ID",
                )
            ]
        )
        captured = await _run_one_tick(_StaticPlanner(out), "group:1")
        state_change = next(
            _values(stmt)["payload"]
            for stmt in captured
            if _values(stmt).get("type") == "agent.task_state_changed"
        )
        self.assertEqual(state_change["task_id"], "EXTERNAL_TASK_ID")
        self.assertEqual(state_change["from_state"], "pending")
        self.assertEqual(state_change["to_state"], "running")

    async def test_inline_and_worker_calls_share_batch_id_and_size(self) -> None:
        out = DecisionOutput(
            actions=[
                CallToolAction(
                    tool_name="task",
                    arguments={"action": "create", "description": "x"},
                ),
                CallToolAction(
                    tool_name="websearch",
                    arguments={"query": "x"},
                ),
            ]
        )
        captured = await _run_one_tick(_StaticPlanner(out), "group:1")
        decision = next(
            _values(stmt)
            for stmt in captured
            if _values(stmt).get("type") == "agent.decision_emitted"
        )
        calls = [
            _values(stmt)["payload"]
            for stmt in captured
            if _values(stmt).get("type") == "agent.tool_called"
        ]
        self.assertEqual(len(calls), 2)
        self.assertEqual(
            {call["tool_batch_id"] for call in calls},
            {decision["event_id"]},
        )
        self.assertEqual([call["tool_batch_size"] for call in calls], [2, 2])


class ValidationFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_persistent_idle_plus_call_retries_then_gives_up(self) -> None:
        out = DecisionOutput(
            actions=[
                IdleAction(reason="nothing"),
                CallToolAction(
                    tool_name="task",
                    arguments={"action": "create", "description": "x"},
                ),
            ]
        )
        planner = _StaticPlanner(out)
        captured = await _run_one_tick(planner, "group:1")
        invalids = [
            _values(stmt)["payload"]
            for stmt in captured
            if _values(stmt).get("type") == "runtime.llm_invalid_output"
        ]
        self.assertEqual([item["attempt"] for item in invalids], [1, 2, 3])
        self.assertNotIn("agent.task_created", _types_after_tick_started(captured))
        idle = next(
            _values(stmt)["payload"]
            for stmt in captured
            if _values(stmt).get("type") == "agent.idle_decision"
        )
        self.assertEqual(idle["reason"], "invalid_output_giveup")

    async def test_invalid_then_valid_second_attempt_applies(self) -> None:
        class _FixOnRetryPlanner:
            def __init__(self) -> None:
                self.contexts: list[DecisionContext] = []

            async def decide(self, context: DecisionContext) -> DecisionOutput:
                self.contexts.append(context)
                if len(self.contexts) == 1:
                    return DecisionOutput(
                        actions=[
                            IdleAction(reason="nothing"),
                            CallToolAction(tool_name="task", arguments={}),
                        ]
                    )
                return DecisionOutput(
                    actions=[
                        CallToolAction(
                            tool_name="task",
                            arguments={
                                "action": "create",
                                "description": "fixed",
                            },
                        )
                    ]
                )

        planner = _FixOnRetryPlanner()
        captured = await _run_one_tick(planner, "group:1")
        self.assertEqual(len(planner.contexts), 2)
        self.assertIn(
            "idle_with_other_actions",
            planner.contexts[1].validation_feedback or "",
        )
        self.assertIn("agent.task_created", _types_after_tick_started(captured))


class CausationChainTests(unittest.IsolatedAsyncioTestCase):
    async def test_inline_task_domain_and_terminal_chain_from_tool_called(self) -> None:
        out = DecisionOutput(
            actions=[
                CallToolAction(
                    tool_name="task",
                    arguments={"action": "create", "description": "x"},
                )
            ]
        )
        captured = await _run_one_tick(_StaticPlanner(out), "group:1")
        rows = [_values(stmt) for stmt in captured]
        self.assertEqual(len({row["correlation_id"] for row in rows}), 1)
        decision = next(row for row in rows if row["type"] == "agent.decision_emitted")
        called = next(row for row in rows if row["type"] == "agent.tool_called")
        created = next(row for row in rows if row["type"] == "agent.task_created")
        terminal = next(row for row in rows if row["type"] == "agent.tool_result")
        self.assertEqual(called["causation_id"], decision["event_id"])
        self.assertEqual(created["causation_id"], called["event_id"])
        self.assertEqual(terminal["causation_id"], called["event_id"])


if __name__ == "__main__":
    unittest.main()

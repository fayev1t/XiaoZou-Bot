"""Contract tests for the effect ``task`` Program API function."""

from __future__ import annotations

import asyncio
import unittest

from qqbot.services.agent_loop.tool_registry import ToolOutcome
from qqbot.services.agent_loop.tools import build_default_registry
from qqbot.services.agent_loop.tools.task import TaskTool


def _run(arguments: dict, **context: object) -> ToolOutcome:
    return asyncio.run(TaskTool().run(arguments, **context))


class TaskToolContractTests(unittest.TestCase):
    def test_registered_as_effect_program_function(self) -> None:
        registry = build_default_registry()
        tool = registry.get("task")
        self.assertIsNotNone(tool)
        self.assertEqual(registry.spec("task").program_kind, "effect")
        self.assertIn("task", registry.names())

    def test_schema_declares_all_four_branches(self) -> None:
        branches = TaskTool.arguments_schema["oneOf"]
        actions = {
            branch["properties"]["action"]["const"] for branch in branches
        }
        self.assertEqual(actions, {"create", "note", "complete", "fail"})

    def test_create_returns_id_and_task_created_event(self) -> None:
        outcome = _run(
            {
                "action": "create",
                "description": "查天气",
                "related_tools": ["websearch"],
            },
            triggered_by_event_id="MSG_1",
        )
        self.assertTrue(outcome.ok)
        self.assertNotIn("task_ref", outcome.result)
        self.assertEqual(outcome.result["state"], "pending")
        self.assertTrue(outcome.result["task_id"])
        self.assertEqual(len(outcome.emitted_events), 1)
        event = outcome.emitted_events[0]
        self.assertEqual(event.event_type, "agent.task_created")
        self.assertEqual(event.payload["task_id"], outcome.result["task_id"])
        self.assertEqual(event.payload["triggered_by_event_id"], "MSG_1")

    def test_note_emits_progress_without_state_change(self) -> None:
        outcome = _run({"action": "note", "task_id": "T1", "note": "已核实"})
        self.assertTrue(outcome.ok)
        event = outcome.emitted_events[0]
        self.assertEqual(event.event_type, "agent.task_progress_noted")
        self.assertEqual(event.payload, {"task_id": "T1", "note": "已核实"})
        self.assertEqual(outcome.result["state"], "unchanged")

    def test_complete_and_fail_emit_terminal_state_changes(self) -> None:
        cases = [
            (
                {
                    "action": "complete",
                    "task_id": "T1",
                    "result_summary": "ok",
                },
                "done",
                "ok",
            ),
            (
                {"action": "fail", "task_id": "T2", "reason": "no data"},
                "failed",
                "no data",
            ),
        ]
        for arguments, state, reason in cases:
            with self.subTest(action=arguments["action"]):
                outcome = _run(arguments)
                self.assertTrue(outcome.ok)
                event = outcome.emitted_events[0]
                self.assertEqual(event.event_type, "agent.task_state_changed")
                self.assertEqual(event.payload["to_state"], state)
                self.assertEqual(event.payload["reason"], reason)
                self.assertEqual(outcome.result["state"], state)

    def test_cross_branch_or_unknown_fields_fail_loudly(self) -> None:
        outcome = _run(
            {
                "action": "complete",
                "task_id": "T1",
                "reason": "wrong branch",
            }
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "invalid_arguments")
        self.assertEqual(outcome.extra["reason_code"], "unknown_fields")
        self.assertEqual(outcome.emitted_events, ())

    def test_missing_required_value_returns_invalid_arguments(self) -> None:
        outcome = _run({"action": "create", "description": ""})
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "invalid_arguments")
        self.assertEqual(outcome.extra["reason_code"], "description_required")


if __name__ == "__main__":
    unittest.main()

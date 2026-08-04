"""Crash-closure contracts for half-written effect calls and programs."""

# Lightweight async protocol doubles deliberately accept generic call shapes.
# ruff: noqa: ANN001, ANN002, ANN003, ARG001, ARG002, PYI034

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from qqbot.services.agent_loop.decision import DecisionContext, DecisionOutput
from qqbot.services.agent_loop.loop import AgentLoop
from qqbot.services.agent_loop.program_events import (
    RecoveryReport,
    _load_recovery_rows,
    recover_interrupted_programs,
)
from qqbot.services.agent_loop.tool_registry import ToolRegistry


def _row(
    event_id: str,
    event_type: str,
    *,
    causation_id: str | None = None,
    payload: dict | None = None,
    correlation_id: str = "CORR",
):
    return SimpleNamespace(
        event_id=event_id,
        type=event_type,
        causation_id=causation_id,
        payload=payload or {},
        correlation_id=correlation_id,
    )


class ProgramRecoveryContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_pending_call_and_program_close_without_replay(self) -> None:
        rows = [
            _row(
                "D1",
                "agent.decision_emitted",
                payload={"program": 'notify(message="x")', "program_sha256": "SHA"},
            ),
            _row(
                "C1",
                "agent.tool_called",
                causation_id="D1",
                payload={
                    "tool_call_id": "TC1",
                    "tool_name": "notify",
                    "task_id": "T1",
                },
            ),
        ]
        session_factory = object()
        with (
            patch(
                "qqbot.services.agent_loop.program_events._load_recovery_rows",
                new=AsyncMock(return_value=rows),
            ) as load_rows,
            patch(
                "qqbot.services.agent_loop.program_events.write_agent_event",
                new=AsyncMock(return_value="TOOL_TERMINAL"),
            ) as write_tool,
            patch(
                "qqbot.services.agent_loop.program_events.write_program_failed",
                new=AsyncMock(return_value="PROGRAM_TERMINAL"),
            ) as write_program,
        ):
            report = await recover_interrupted_programs(
                session_factory, scope_key="group:7"
            )

        self.assertEqual(report, RecoveryReport(tool_calls_closed=1, programs_closed=1))
        load_rows.assert_awaited_once_with(session_factory, scope_key="group:7")
        write_tool.assert_awaited_once()
        tool_kwargs = write_tool.await_args.kwargs
        self.assertEqual(tool_kwargs["causation_id"], "C1")
        self.assertEqual(tool_kwargs["payload"]["error_kind"], "interrupted")
        self.assertEqual(tool_kwargs["payload"]["status"], "uncertain")
        self.assertIn("not replayed", tool_kwargs["payload"]["error_message"])
        write_program.assert_awaited_once()
        program_kwargs = write_program.await_args.kwargs
        self.assertEqual(program_kwargs["decision_id"], "D1")
        self.assertEqual(program_kwargs["program_sha256"], "SHA")
        self.assertEqual(program_kwargs["effect_call_ids"], ["TC1"])
        self.assertEqual(program_kwargs["error_kind"], "interrupted")
        self.assertEqual(program_kwargs["status"], "uncertain")

    async def test_existing_terminals_are_not_closed_again(self) -> None:
        rows = [
            _row(
                "D1",
                "agent.decision_emitted",
                payload={"program": "# done", "program_sha256": "SHA"},
            ),
            _row(
                "C1",
                "agent.tool_called",
                causation_id="D1",
                payload={"tool_call_id": "TC1", "tool_name": "notify"},
            ),
            _row("T1", "agent.tool_result", causation_id="C1"),
            _row("P1", "agent.program_completed", causation_id="D1"),
        ]
        with (
            patch(
                "qqbot.services.agent_loop.program_events._load_recovery_rows",
                new=AsyncMock(return_value=rows),
            ),
            patch(
                "qqbot.services.agent_loop.program_events.write_agent_event",
                new=AsyncMock(),
            ) as write_tool,
            patch(
                "qqbot.services.agent_loop.program_events.write_program_failed",
                new=AsyncMock(),
            ) as write_program,
        ):
            report = await recover_interrupted_programs(object(), scope_key="group:7")
        self.assertEqual(report, RecoveryReport())
        write_tool.assert_not_awaited()
        write_program.assert_not_awaited()

    async def test_legacy_batched_pending_call_is_closed_by_same_recovery(self) -> None:
        rows = [
            _row(
                "C_OLD",
                "agent.tool_called",
                payload={
                    "tool_call_id": "TC_OLD",
                    "tool_name": "send_messages",
                    "tool_batch_id": "OLD_BATCH",
                    "tool_batch_size": 2,
                },
            )
        ]
        with (
            patch(
                "qqbot.services.agent_loop.program_events._load_recovery_rows",
                new=AsyncMock(return_value=rows),
            ),
            patch(
                "qqbot.services.agent_loop.program_events.write_agent_event",
                new=AsyncMock(return_value="CLOSED"),
            ) as write_tool,
            patch(
                "qqbot.services.agent_loop.program_events.write_program_failed",
                new=AsyncMock(),
            ),
        ):
            report = await recover_interrupted_programs(object(), scope_key="group:7")
        self.assertEqual(report.tool_calls_closed, 1)
        self.assertEqual(
            write_tool.await_args.kwargs["payload"]["tool_call_id"], "TC_OLD"
        )

    async def test_database_query_is_filtered_to_requested_scope(self) -> None:
        captured: list[object] = []

        class _Result:
            def scalars(self) -> "_Result":
                return self

            def all(self) -> list:
                return []

        class _Session:
            async def execute(self, statement):
                captured.append(statement)
                return _Result()

            async def __aenter__(self) -> "_Session":
                return self

            async def __aexit__(self, *args) -> None:
                return None

        await _load_recovery_rows(_Session, "group:77")
        sql = str(captured[0])
        self.assertIn("agent_events.scope", sql)
        self.assertIn("agent_events.group_id", sql)
        self.assertNotIn("agent_events.user_id =", sql)


class RecoveryOrderingContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_tick_recovers_before_projection_and_only_once(self) -> None:
        order: list[str] = []

        class _Planner:
            async def decide(self, context: DecisionContext) -> DecisionOutput:
                return DecisionOutput(program="# idle")

        loop = AgentLoop(
            scope_key="group:1",
            planner=_Planner(),
            session_factory=object(),
            tool_registry=ToolRegistry(),
        )

        async def _recover(*args, **kwargs) -> RecoveryReport:
            order.append("recover")
            return RecoveryReport()

        async def _build_context(*args, **kwargs) -> DecisionContext:
            order.append("project")
            return DecisionContext(
                scope_key="group:1",
                correlation_id=args[0],
                tick_seq=loop._tick_seq,
                now=args[1],
            )

        with (
            patch(
                "qqbot.services.agent_loop.loop.recover_interrupted_programs",
                new=_recover,
            ),
            patch.object(loop, "_build_context", new=_build_context),
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
                new=AsyncMock(return_value="PROGRAM"),
            ),
        ):
            await loop._tick()
            await loop._tick()

        self.assertEqual(order, ["recover", "project", "project"])
        self.assertTrue(loop._recovery_done)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

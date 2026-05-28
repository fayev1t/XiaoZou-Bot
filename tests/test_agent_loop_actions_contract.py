"""Contract tests for the v2 action translation layer.

Covers (任务与决策契约 §3.1, §3.2, §7.1):
- IdleAction          → agent.idle_decision
- CreateTaskAction    → agent.task_created (+ records task_ref for in-tick reuse)
- CallToolAction      → agent.tool_called (+ auto agent.task_state_changed pending→running)
- CallToolAction with task_ref resolves to the just-minted task_id
- CompleteTaskAction  → agent.task_state_changed (to=done)
- FailTaskAction      → agent.task_state_changed (to=failed)
- Validation: idle + other forces runtime.llm_invalid_output + auto-idle

Reply is no longer a first-class action — it is invoked via CallToolAction
with tool_name="reply" and dispatched through ToolWorker → ReplyTool. The
target.kind/group_id mismatch and other reply-specific validations are now
covered in test_reply_tool_contract.py.

Pure unit-level: a recording session captures every insert.
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from typing import Any

from qqbot.services.agent_loop import (
    AgentLoop,
    CallToolAction,
    CompleteTaskAction,
    CreateTaskAction,
    DecisionContext,
    DecisionOutput,
    FailTaskAction,
    IdleAction,
    NoteTaskProgressAction,
)


class _RecordingSession:
    def __init__(self, store: list[Any]) -> None:
        self._store = store

    async def execute(self, stmt: Any) -> Any:
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


def _types_after_tick_started(captured: list[Any]) -> list[str]:
    """Return event types written within a tick, skipping the leading
    runtime.tick_started so individual tests can focus on the action layer."""
    types = [_values(stmt).get("type") for stmt in captured]
    # filter out tick_started/ended which all ticks emit
    return [t for t in types if t not in ("runtime.tick_started", "runtime.tick_ended")]


class _StaticPlanner:
    def __init__(self, output: DecisionOutput) -> None:
        self._output = output

    async def decide(self, context: DecisionContext) -> DecisionOutput:
        _ = context
        return self._output


async def _run_one_tick(planner: _StaticPlanner, scope_key: str) -> list[Any]:
    captured: list[Any] = []
    loop = AgentLoop(
        scope_key=scope_key,
        planner=planner,
        session_factory=_factory_for(captured),
    )
    loop.start()
    loop.wake()
    # Give the tick room to complete.
    for _ in range(80):
        await asyncio.sleep(0.01)
        # tick_started + decision_emitted + 至少 1 action + tick_ended
        if len(captured) >= 4:
            await asyncio.sleep(0.02)  # ensure trailing events settled
            break
    await loop.stop()
    return captured


class IdleActionTranslationTests(unittest.IsolatedAsyncioTestCase):
    async def test_idle_writes_agent_idle_decision(self) -> None:
        out = DecisionOutput(actions=[IdleAction(reason="nothing-to-do")])
        captured = await _run_one_tick(_StaticPlanner(out), "group:1")
        types = _types_after_tick_started(captured)
        self.assertIn("agent.idle_decision", types)
        # reason 透传到 payload
        idle = next(
            stmt for stmt in captured
            if _values(stmt).get("type") == "agent.idle_decision"
        )
        self.assertEqual(_values(idle)["payload"]["reason"], "nothing-to-do")


class CreateTaskActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_task_writes_agent_task_created(self) -> None:
        out = DecisionOutput(actions=[
            CreateTaskAction(description="check weather", related_tools=["web_search"]),
        ])
        captured = await _run_one_tick(_StaticPlanner(out), "group:1")
        types = _types_after_tick_started(captured)
        self.assertIn("agent.task_created", types)
        created = next(
            stmt for stmt in captured
            if _values(stmt).get("type") == "agent.task_created"
        )
        payload = _values(created)["payload"]
        self.assertEqual(payload["description"], "check weather")
        self.assertEqual(payload["related_tools"], ["web_search"])
        self.assertIsNotNone(payload["task_id"])


class CallToolActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_call_tool_writes_agent_tool_called(self) -> None:
        out = DecisionOutput(actions=[
            CallToolAction(tool_name="web_search", arguments={"q": "x"}),
        ])
        captured = await _run_one_tick(_StaticPlanner(out), "group:1")
        types = _types_after_tick_started(captured)
        self.assertIn("agent.tool_called", types)
        # No task_id linkage → no state_changed
        self.assertNotIn("agent.task_state_changed", types)
        called = next(
            stmt for stmt in captured
            if _values(stmt).get("type") == "agent.tool_called"
        )
        payload = _values(called)["payload"]
        self.assertEqual(payload["tool_name"], "web_search")
        self.assertEqual(payload["arguments"], {"q": "x"})
        self.assertIsNone(payload["task_id"])

    async def test_call_tool_with_task_id_auto_advances_state(self) -> None:
        out = DecisionOutput(actions=[
            CallToolAction(
                tool_name="web_search",
                arguments={"q": "x"},
                task_id="EXTERNAL_TASK_ID",
            ),
        ])
        captured = await _run_one_tick(_StaticPlanner(out), "group:1")
        types = _types_after_tick_started(captured)
        self.assertEqual(
            types,
            ["agent.decision_emitted", "agent.tool_called", "agent.task_state_changed"],
        )
        sc = next(
            stmt for stmt in captured
            if _values(stmt).get("type") == "agent.task_state_changed"
        )
        payload = _values(sc)["payload"]
        self.assertEqual(payload["task_id"], "EXTERNAL_TASK_ID")
        self.assertEqual(payload["from_state"], "pending")
        self.assertEqual(payload["to_state"], "running")

    async def test_task_ref_resolves_to_just_minted_task_id(self) -> None:
        out = DecisionOutput(actions=[
            CreateTaskAction(description="x", task_ref="r1"),
            CallToolAction(tool_name="web_search", arguments={}, task_ref="r1"),
        ])
        captured = await _run_one_tick(_StaticPlanner(out), "group:1")
        created = next(
            stmt for stmt in captured
            if _values(stmt).get("type") == "agent.task_created"
        )
        called = next(
            stmt for stmt in captured
            if _values(stmt).get("type") == "agent.tool_called"
        )
        state_chg = next(
            stmt for stmt in captured
            if _values(stmt).get("type") == "agent.task_state_changed"
        )
        new_task_id = _values(created)["payload"]["task_id"]
        self.assertEqual(_values(called)["payload"]["task_id"], new_task_id)
        self.assertEqual(_values(state_chg)["payload"]["task_id"], new_task_id)


class ReplyAsToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_reply_now_routes_as_tool_call(self) -> None:
        """Reply 不再走专门的 ReplyAction 分支，而是普通的 CallToolAction —
        loop 写 agent.tool_called，ToolWorker 后续执行 ReplyTool 才写
        agent.reply_emitted。这里只断言 loop 层不再写 reply_emitted。"""
        out = DecisionOutput(actions=[
            CallToolAction(
                tool_name="reply",
                arguments={
                    "content": [{"type": "text", "data": {"text": "hi"}}],
                    "target": {"kind": "group", "group_id": 12345},
                },
            ),
        ])
        captured = await _run_one_tick(_StaticPlanner(out), "group:12345")
        types = _types_after_tick_started(captured)
        self.assertIn("agent.tool_called", types)
        # loop 层不再直接落 reply_emitted —— 它由 ReplyTool.run() 在
        # ToolWorker 里发出。
        self.assertNotIn("agent.reply_emitted", types)
        # 校验通过：没有 invalid_output 事件
        self.assertNotIn("runtime.llm_invalid_output", types)
        called = next(
            stmt for stmt in captured
            if _values(stmt).get("type") == "agent.tool_called"
        )
        payload = _values(called)["payload"]
        self.assertEqual(payload["tool_name"], "reply")
        self.assertEqual(
            payload["arguments"]["target"],
            {"kind": "group", "group_id": 12345},
        )


class CompleteAndFailTaskTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_task_emits_state_change_done(self) -> None:
        out = DecisionOutput(actions=[
            CompleteTaskAction(task_id="T1", result_summary="ok"),
        ])
        captured = await _run_one_tick(_StaticPlanner(out), "group:1")
        sc = next(
            stmt for stmt in captured
            if _values(stmt).get("type") == "agent.task_state_changed"
        )
        payload = _values(sc)["payload"]
        self.assertEqual(payload["task_id"], "T1")
        self.assertEqual(payload["to_state"], "done")
        self.assertEqual(payload["reason"], "ok")

    async def test_fail_task_emits_state_change_failed(self) -> None:
        out = DecisionOutput(actions=[
            FailTaskAction(task_id="T1", reason="search empty"),
        ])
        captured = await _run_one_tick(_StaticPlanner(out), "group:1")
        sc = next(
            stmt for stmt in captured
            if _values(stmt).get("type") == "agent.task_state_changed"
        )
        payload = _values(sc)["payload"]
        self.assertEqual(payload["task_id"], "T1")
        self.assertEqual(payload["to_state"], "failed")
        self.assertEqual(payload["reason"], "search empty")


class NoteTaskProgressActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_note_emits_task_progress_noted_event(self) -> None:
        out = DecisionOutput(actions=[
            NoteTaskProgressAction(task_id="T1", note="找到了张三的发言"),
        ])
        captured = await _run_one_tick(_StaticPlanner(out), "group:1")
        types = _types_after_tick_started(captured)
        # note 不改 task state，所以不应有 task_state_changed
        self.assertIn("agent.task_progress_noted", types)
        self.assertNotIn("agent.task_state_changed", types)

        pn = next(
            stmt for stmt in captured
            if _values(stmt).get("type") == "agent.task_progress_noted"
        )
        payload = _values(pn)["payload"]
        self.assertEqual(payload["task_id"], "T1")
        self.assertEqual(payload["note"], "找到了张三的发言")


class CreateTaskTriggeredByEventIdTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_task_persists_triggered_by_event_id(self) -> None:
        out = DecisionOutput(actions=[
            CreateTaskAction(description="d", triggered_by_event_id="MSG_99"),
        ])
        captured = await _run_one_tick(_StaticPlanner(out), "group:1")
        tc = next(
            stmt for stmt in captured
            if _values(stmt).get("type") == "agent.task_created"
        )
        self.assertEqual(
            _values(tc)["payload"]["triggered_by_event_id"], "MSG_99"
        )


class ValidationFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_idle_with_other_action_forces_idle(self) -> None:
        out = DecisionOutput(actions=[
            IdleAction(reason="nothing"),
            CreateTaskAction(description="x"),
        ])
        captured = await _run_one_tick(_StaticPlanner(out), "group:1")
        types = _types_after_tick_started(captured)
        self.assertIn("runtime.llm_invalid_output", types)
        # task_created should NOT be emitted
        self.assertNotIn("agent.task_created", types)
        inv = next(
            stmt for stmt in captured
            if _values(stmt).get("type") == "runtime.llm_invalid_output"
        )
        self.assertEqual(
            _values(inv)["payload"]["validation_error"], "idle_with_other_actions"
        )


class CausationChainTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_action_events_share_correlation_and_chain_from_decision(
        self,
    ) -> None:
        out = DecisionOutput(actions=[
            CreateTaskAction(description="x", task_ref="r"),
            CallToolAction(tool_name="x", task_ref="r"),
        ])
        captured = await _run_one_tick(_StaticPlanner(out), "group:1")
        rows = [_values(s) for s in captured]
        corrs = {r["correlation_id"] for r in rows}
        self.assertEqual(len(corrs), 1)  # 同一 tick

        decision = next(r for r in rows if r["type"] == "agent.decision_emitted")
        for r in rows:
            if r["type"] in (
                "agent.task_created",
                "agent.tool_called",
            ):
                self.assertEqual(r["causation_id"], decision["event_id"])

        # task_state_changed pending→running 由 tool_called 触发
        called = next(r for r in rows if r["type"] == "agent.tool_called")
        sc = next(r for r in rows if r["type"] == "agent.task_state_changed")
        self.assertEqual(sc["causation_id"], called["event_id"])


if __name__ == "__main__":
    unittest.main()

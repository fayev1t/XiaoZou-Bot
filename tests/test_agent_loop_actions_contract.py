"""Contract tests for the v2 action translation layer.

Covers (任务与决策契约 §3.1, §3.2, §7.1):
- IdleAction          → agent.idle_decision
- CreateTaskAction    → agent.task_created (+ records task_ref for in-tick reuse)
- CallToolAction      → agent.tool_called (+ auto agent.task_state_changed pending→running)
- CallToolAction with task_ref resolves to the just-minted task_id
- CompleteTaskAction  → agent.task_state_changed (to=done)
- FailTaskAction      → agent.task_state_changed (to=failed)
- Validation: idle + other → 同 tick 带 validation_feedback 重试至多 2 次
  （每次失败写 runtime.llm_invalid_output，attempt 递增）；三次全败才强制
  idle(reason="invalid_output_giveup")（契约 §7.1，2026-07-02 落地）

Reply is no longer a first-class action — it is invoked via CallToolAction
with tool_name="send_message" and dispatched through ToolWorker →
SendMessageTool. The target.kind/group_id mismatch and other
send_message-specific validations are now covered in
test_send_message_tool_contract.py.

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


def _is_event_stmt(stmt: Any) -> bool:
    """只保留写 agent_events 的语句。

    自 2026-06-22 起，写 agent.task_* 事件会**额外**触发 agent_tasks 读模型双写
    （task_store.apply_task_event_safe，走同一 session_factory），于是 recording
    session 会一并捕获到 agent_tasks 的 INSERT/UPDATE。本测试只关心"动作→事件流"
    的翻译，故在入口处把非 agent_events 语句过滤掉；读模型本身在
    test_task_store_contract.py 单独覆盖。"""
    table = getattr(stmt, "table", None)
    return table is not None and getattr(table, "name", None) == "agent_events"


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
    # 只回事件流语句 —— 过滤掉 task 事件触发的 agent_tasks 读模型双写副作用。
    return [s for s in captured if _is_event_stmt(s)]


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

    async def test_same_tick_tool_calls_share_batch_id_and_size(self) -> None:
        """同一 tick 的全部 call_tool 打同一 tool_batch_id（=decision_id）+
        tool_batch_size —— ToolWorker 据此判定批次收口（批次语义 2026-07-01）。"""
        out = DecisionOutput(actions=[
            CallToolAction(tool_name="send_message", arguments={}),
            CallToolAction(tool_name="websearch", arguments={"q": "x"}),
        ])
        captured = await _run_one_tick(_StaticPlanner(out), "group:1")
        decision = next(
            stmt for stmt in captured
            if _values(stmt).get("type") == "agent.decision_emitted"
        )
        decision_id = _values(decision)["event_id"]
        calls = [
            _values(stmt)["payload"]
            for stmt in captured
            if _values(stmt).get("type") == "agent.tool_called"
        ]
        self.assertEqual(len(calls), 2)
        self.assertEqual(
            {c["tool_batch_id"] for c in calls}, {decision_id}
        )
        self.assertEqual([c["tool_batch_size"] for c in calls], [2, 2])


class SendMessageAsToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_reply_now_routes_as_tool_call(self) -> None:
        """发言不再走专门的 ReplyAction 分支，而是普通的 CallToolAction —
        loop 写 agent.tool_called，ToolWorker 后续同步执行 SendMessageTool 直接
        发送。这里只断言 loop 层不写 reply_emitted。"""
        out = DecisionOutput(actions=[
            CallToolAction(
                tool_name="send_message",
                arguments={
                    "content": [{"type": "text", "data": {"text": "hi"}}],
                    "target": {"kind": "group", "group_id": 12345},
                },
            ),
        ])
        captured = await _run_one_tick(_StaticPlanner(out), "group:12345")
        types = _types_after_tick_started(captured)
        self.assertIn("agent.tool_called", types)
        # loop 层不落 reply_emitted —— 发送由 SendMessageTool.run() 在
        # ToolWorker 里同步完成。
        self.assertNotIn("agent.reply_emitted", types)
        # 校验通过：没有 invalid_output 事件
        self.assertNotIn("runtime.llm_invalid_output", types)
        called = next(
            stmt for stmt in captured
            if _values(stmt).get("type") == "agent.tool_called"
        )
        payload = _values(called)["payload"]
        self.assertEqual(payload["tool_name"], "send_message")
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
    async def test_persistent_invalid_retries_then_gives_up(self) -> None:
        """契约 §7.1（2026-07-02 落地）：非法输出同 tick 重试至多 2 次（共 3
        次调用），每次失败写一条 llm_invalid_output（attempt 递增）；三次仍
        非法才强制 idle(reason="invalid_output_giveup")。"""
        out = DecisionOutput(actions=[
            IdleAction(reason="nothing"),
            CreateTaskAction(description="x"),
        ])
        planner = _StaticPlanner(out)  # 每次都返回同样的非法输出
        captured = await _run_one_tick(planner, "group:1")
        types = _types_after_tick_started(captured)
        # task_created should NOT be emitted
        self.assertNotIn("agent.task_created", types)
        invalids = [
            _values(stmt)["payload"]
            for stmt in captured
            if _values(stmt).get("type") == "runtime.llm_invalid_output"
        ]
        self.assertEqual(len(invalids), 3)
        self.assertEqual([p["attempt"] for p in invalids], [1, 2, 3])
        for p in invalids:
            self.assertEqual(p["validation_error"], "idle_with_other_actions")
        # 最终强制 idle，reason 按契约固定为 invalid_output_giveup
        idle = next(
            stmt for stmt in captured
            if _values(stmt).get("type") == "agent.idle_decision"
        )
        self.assertEqual(
            _values(idle)["payload"]["reason"], "invalid_output_giveup"
        )

    async def test_invalid_then_valid_second_attempt_applies(self) -> None:
        """重试带反馈：第二次调用的 context 必须携带 validation_feedback；
        修好后动作正常落地，只留一条 invalid 事件，不强制 idle。"""

        class _FixOnRetryPlanner:
            def __init__(self) -> None:
                self.contexts: list[DecisionContext] = []

            async def decide(self, context: DecisionContext) -> DecisionOutput:
                self.contexts.append(context)
                if len(self.contexts) == 1:
                    return DecisionOutput(actions=[
                        IdleAction(reason="nothing"),
                        CreateTaskAction(description="x"),
                    ])
                return DecisionOutput(actions=[
                    CreateTaskAction(description="fixed"),
                ])

        planner = _FixOnRetryPlanner()
        captured = await _run_one_tick(planner, "group:1")  # type: ignore[arg-type]
        types = _types_after_tick_started(captured)

        self.assertEqual(len(planner.contexts), 2)
        self.assertIsNone(planner.contexts[0].validation_feedback)
        self.assertIn(
            "idle_with_other_actions",
            planner.contexts[1].validation_feedback or "",
        )
        invalids = [
            s for s in captured
            if _values(s).get("type") == "runtime.llm_invalid_output"
        ]
        self.assertEqual(len(invalids), 1)
        self.assertIn("agent.task_created", types)
        self.assertNotIn("agent.idle_decision", types)


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

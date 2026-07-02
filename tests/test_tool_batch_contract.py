"""Contract tests for tool batch semantics（批次收口的批次级唤醒）.

设计结论（2026-07-01 引入批次；2026-07-02 拆除门闩）：
- 一次 tick 里发出的所有工具调用属于同一个 tool_batch（id 复用 decision_id，
  size = 本拍 call_tool 个数），批次标记由 AgentLoop 写进 tool_called.payload
  ——工具本身保持黑盒，对批次一无所知。
- ToolWorker 判定"整批全部 terminal 且条数 ≥ batch_size"后，先写
  runtime.tool_batch_completed 标记事件，再经 notify_tool_batch_completed
  唤醒一次——批次级一次，不是工具级一次。
- **没有门闩**（2026-07-02，模型+prompt 优先哲学）：批次进行期间的任何唤醒
  随时开拍——即便注入的 supervisor 还残留 has_open_tool_batch 接口，loop 也
  不问不理；AgentLoop 亦不再调用 notify_tool_batch_opened。防复读靠 prompt
  （processing 行语义），不靠程序闸门。

对应契约：任务与决策契约.md §5.3（批次级唤醒）、事件系统设计.md（
runtime.tool_batch_completed 事件登记）。
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from qqbot.services.agent_loop import FakeIdlePlanner
from qqbot.services.agent_loop.decision import (
    CallToolAction,
    DecisionContext,
    DecisionOutput,
)
from qqbot.services.agent_loop.loop import AgentLoop
from qqbot.services.agent_loop.supervisor import LoopSupervisor
from qqbot.services.agent_loop.tool_registry import ToolRegistry
from qqbot.services.agent_loop.tool_worker import ToolWorker


# ─── 共用 fakes ───


class _EmptyResult:
    def scalars(self) -> "_EmptyResult":
        return self

    def all(self) -> list:
        return []

    def mappings(self) -> "_EmptyResult":
        return self

    def first(self) -> None:
        return None


class _RecordingSession:
    """记录 INSERT 语句；SELECT 一律回空（供 supervisor.start 的 backfill 等）。"""

    def __init__(self, store: list[Any]) -> None:
        self._store = store

    async def execute(self, stmt: Any) -> Any:
        if getattr(stmt, "is_select", False):
            return _EmptyResult()
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


def _values_of(stmt: Any) -> dict:
    try:
        return stmt.compile().params
    except Exception:
        return {}


class _RecordingLoop:
    """替身 AgentLoop：只数 wake 次数。"""

    def __init__(self) -> None:
        self.wakes = 0

    def wake(self) -> None:
        self.wakes += 1

    async def stop(self) -> None:
        return None


# ─── supervisor 批次级唤醒（无门闩簿记）───


class SupervisorBatchWakeTests(unittest.IsolatedAsyncioTestCase):
    def _supervisor_with_fake_loop(
        self, scope_key: str
    ) -> tuple[LoopSupervisor, _RecordingLoop]:
        sup = LoopSupervisor(
            planner=FakeIdlePlanner(),
            session_factory=_factory_for([]),
        )
        fake = _RecordingLoop()
        sup._loops[scope_key] = fake  # type: ignore[assignment]
        return sup, fake

    async def test_batch_completed_wakes_exactly_once(self) -> None:
        sup, fake = self._supervisor_with_fake_loop("group:1")
        await sup.notify_tool_batch_completed("group:1", "B1")
        self.assertEqual(fake.wakes, 1)

    async def test_latch_interfaces_removed(self) -> None:
        # 2026-07-02 门闩拆除：上闩/查闩接口不得复活（防止有人把程序级
        # "何时可以思考"的闸门加回来——防复读归 prompt 管）。
        sup, _fake = self._supervisor_with_fake_loop("group:1")
        self.assertFalse(hasattr(sup, "notify_tool_batch_opened"))
        self.assertFalse(hasattr(sup, "has_open_tool_batch"))


# ─── AgentLoop：任何唤醒随时开拍（无门闩）───


class _LegacyLatchSupervisor:
    """残留旧门闩接口的鸭子 supervisor——loop 必须完全无视这些接口。"""

    def __init__(self) -> None:
        self.opened: list[tuple[str, str]] = []

    def notify_tool_pending(self) -> None:
        return None

    def notify_tool_batch_opened(self, scope_key: str, batch_id: str) -> None:
        self.opened.append((scope_key, batch_id))

    def has_open_tool_batch(self, scope_key: str) -> bool:
        return True  # 永远声称"批次未收口"——loop 不该问它


class AgentLoopNoBatchGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_wake_ticks_even_if_supervisor_claims_open_batch(
        self,
    ) -> None:
        """核心断言（2026-07-02 门闩拆除）：即便 supervisor 声称批次未收口，
        唤醒也要正常开拍——程序不再替模型决定"何时可以思考"。"""
        captured: list[Any] = []
        sup = _LegacyLatchSupervisor()
        loop = AgentLoop(
            scope_key="group:1",
            planner=FakeIdlePlanner(),
            session_factory=_factory_for(captured),
            supervisor=sup,
        )
        loop.start()
        loop.wake()
        for _ in range(50):
            await asyncio.sleep(0.01)
            if len(captured) >= 4:
                break
        await loop.stop()
        types = [_values_of(stmt).get("type") for stmt in captured]
        self.assertIn("runtime.tick_started", types)
        self.assertIn("runtime.tick_ended", types)

    async def test_loop_never_opens_latch_but_still_stamps_batch(self) -> None:
        """派发多工具时：不得调用 notify_tool_batch_opened（门闩已死），但
        tool_called.payload 的批次标记（tool_batch_id/size）必须保留——它是
        ToolWorker 批次收口判定的依据。"""
        captured: list[Any] = []

        class _TwoToolPlanner:
            async def decide(self, context: DecisionContext) -> DecisionOutput:
                return DecisionOutput(
                    actions=[
                        CallToolAction(tool_name="a", arguments={}),
                        CallToolAction(tool_name="b", arguments={}),
                    ]
                )

        sup = _LegacyLatchSupervisor()
        loop = AgentLoop(
            scope_key="group:1",
            planner=_TwoToolPlanner(),
            session_factory=_factory_for(captured),
            supervisor=sup,
        )
        loop.start()
        loop.wake()
        for _ in range(80):
            await asyncio.sleep(0.01)
            types = [_values_of(stmt).get("type") for stmt in captured]
            if "runtime.tick_ended" in types:
                break
        await loop.stop()

        self.assertEqual(sup.opened, [])
        called_payloads = [
            _values_of(stmt)["payload"]
            for stmt in captured
            if _values_of(stmt).get("type") == "agent.tool_called"
        ]
        self.assertEqual(len(called_payloads), 2)
        for payload in called_payloads:
            self.assertTrue(payload.get("tool_batch_id"))
            self.assertEqual(payload.get("tool_batch_size"), 2)


# ─── ToolWorker 批次收口 ───


class _BatchHarness:
    """脚本化 DB：pending 行 / 批次计数 / completion 存在性由测试给定。"""

    def __init__(
        self,
        pending_rows: list[dict],
        batch_status: dict[str, dict],
        completed_exists: bool = False,
    ) -> None:
        self.pending_rows = pending_rows
        self.batch_status = batch_status
        self.completed_exists = completed_exists
        # INSERT 语句与 supervisor 通知按发生顺序混录，供断言"先写事件再唤醒"
        self.timeline: list[Any] = []


class _ScriptResult:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def mappings(self) -> "_ScriptResult":
        return self

    def all(self) -> list[dict]:
        return self._rows

    def first(self) -> dict | None:
        return self._rows[0] if self._rows else None


class _BatchSession:
    def __init__(self, harness: _BatchHarness) -> None:
        self._h = harness

    async def execute(self, stmt: Any, params: dict | None = None) -> Any:
        sql = getattr(stmt, "text", None)
        if isinstance(sql, str):
            if "runtime.tool_batch_completed" in sql:
                return _ScriptResult(
                    [{"exists": 1}] if self._h.completed_exists else []
                )
            if "tool_batch_id" in sql:
                status = self._h.batch_status.get(
                    (params or {}).get("tool_batch_id", ""),
                    {"called": 0, "terminal": 0},
                )
                return _ScriptResult([status])
            return _ScriptResult(self._h.pending_rows)
        self._h.timeline.append(stmt)
        return SimpleNamespace(rowcount=1)

    async def commit(self) -> None:
        return None

    async def __aenter__(self) -> "_BatchSession":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None


class _BatchSupervisor:
    """记录 wake / notify_tool_batch_completed，与 harness.timeline 共序。"""

    def __init__(self, harness: _BatchHarness) -> None:
        self._h = harness
        self.woke: list[str] = []
        self.batch_completed: list[tuple[str, str]] = []

    async def wake(self, scope_key: str) -> None:
        self.woke.append(scope_key)
        self._h.timeline.append(("wake", scope_key))

    async def notify_tool_batch_completed(
        self, scope_key: str, tool_batch_id: str
    ) -> None:
        self.batch_completed.append((scope_key, tool_batch_id))
        self._h.timeline.append(("batch_completed", scope_key, tool_batch_id))


class _StubTool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = "stub"
        self.arguments_schema = {"type": "object"}

    async def run(self, arguments: dict, **_: Any) -> Any:
        return {"ok": True}


def _batched_row(
    event_id: str,
    tool_call_id: str,
    *,
    group_id: int = 100,
    batch_id: str | None = "B1",
    batch_size: int | None = 2,
) -> dict:
    payload: dict[str, Any] = {
        "tool_call_id": tool_call_id,
        "tool_name": "websearch",
        "arguments": {},
    }
    if batch_id is not None:
        payload["tool_batch_id"] = batch_id
        payload["tool_batch_size"] = batch_size
    return {
        "event_id": event_id,
        "scope": "group",
        "group_id": group_id,
        "user_id": None,
        "correlation_id": "CID",
        "payload": payload,
    }


def _completion_inserts(harness: _BatchHarness) -> list[Any]:
    return [
        s
        for s in harness.timeline
        if not isinstance(s, tuple)
        and _values_of(s).get("type") == "runtime.tool_batch_completed"
    ]


def _worker_for(harness: _BatchHarness) -> tuple[ToolWorker, _BatchSupervisor]:
    reg = ToolRegistry()
    reg.register(_StubTool("websearch"))
    sup = _BatchSupervisor(harness)

    def factory() -> _BatchSession:
        return _BatchSession(harness)

    worker = ToolWorker(
        session_factory=factory, registry=reg, supervisor=sup
    )
    worker._schedule_retry = lambda *_a, **_k: None  # type: ignore[method-assign]
    return worker, sup


class ToolWorkerBatchCloseTests(unittest.TestCase):
    def test_partial_batch_does_not_wake(self) -> None:
        """核心断言①：同 batch 两个工具一先一后完成，先完成的那个不得触发
        唤醒（也不写 completion 事件）——中间不能开新 tick。"""
        rows = [
            _batched_row("EID1", "TC1"),
            _batched_row("EID2", "TC2"),
        ]
        # EID2 的 claim 被占（模拟它还在别处执行/未完成）→ 本轮只有 EID1 terminal
        harness = _BatchHarness(
            pending_rows=rows,
            batch_status={"B1": {"called": 2, "terminal": 1}},
        )
        worker, sup = _worker_for(harness)

        from qqbot.services.agent_loop.delivery_claims import ClaimResult

        async def _claim(_sf: Any, event_id: str, _kind: str) -> ClaimResult:
            return ClaimResult(
                claimed=(event_id != "EID2"), retry_after_seconds=5.0
            )

        with patch(
            "qqbot.services.agent_loop.tool_worker.claim_delivery",
            new=AsyncMock(side_effect=_claim),
        ):
            asyncio.run(worker._drain_once())

        self.assertEqual(_completion_inserts(harness), [])
        self.assertEqual(sup.batch_completed, [])
        self.assertEqual(sup.woke, [])

    def test_full_batch_completion_event_then_single_wake(self) -> None:
        """核心断言②：整批 terminal 后先写 runtime.tool_batch_completed，
        再解闩唤醒——且只唤醒一次。"""
        rows = [
            _batched_row("EID1", "TC1"),
            _batched_row("EID2", "TC2"),
        ]
        harness = _BatchHarness(
            pending_rows=rows,
            batch_status={"B1": {"called": 2, "terminal": 2}},
        )
        worker, sup = _worker_for(harness)
        asyncio.run(worker._drain_once())

        completions = _completion_inserts(harness)
        self.assertEqual(len(completions), 1)
        params = _values_of(completions[0])
        # agent_visible：标记要进模型 timeline（渲染成 <system-hint>），
        # 让模型显式看到批次边界——不是只给调度层用的哑标记。
        self.assertEqual(params.get("visibility"), "agent_visible")
        self.assertEqual(params["payload"]["tool_batch_id"], "B1")
        self.assertEqual(params["payload"]["tool_count"], 2)
        # 批次级通知恰好一次；不走 per-scope 直接 wake
        self.assertEqual(sup.batch_completed, [("group:100", "B1")])
        self.assertEqual(sup.woke, [])
        # 顺序：completion 事件必须先于解闩通知落 timeline
        completed_idx = harness.timeline.index(completions[0])
        notify_idx = harness.timeline.index(
            ("batch_completed", "group:100", "B1")
        )
        self.assertLess(completed_idx, notify_idx)

    def test_write_race_called_below_size_does_not_close(self) -> None:
        """AgentLoop 还没写完同批后续 tool_called（called < batch_size）时不得
        收口——防 drain 撞进写间隙的竞态。"""
        rows = [_batched_row("EID1", "TC1", batch_size=2)]
        harness = _BatchHarness(
            pending_rows=rows,
            batch_status={"B1": {"called": 1, "terminal": 1}},
        )
        worker, sup = _worker_for(harness)
        asyncio.run(worker._drain_once())

        self.assertEqual(_completion_inserts(harness), [])
        self.assertEqual(sup.batch_completed, [])
        self.assertEqual(sup.woke, [])

    def test_existing_completion_not_rewritten_but_still_notifies(self) -> None:
        # completion 已存在（上次写完后进程在唤醒前挂了）→ 不重写事件，但仍
        # 补发解闩通知。
        rows = [_batched_row("EID1", "TC1", batch_size=1)]
        harness = _BatchHarness(
            pending_rows=rows,
            batch_status={"B1": {"called": 1, "terminal": 1}},
            completed_exists=True,
        )
        worker, sup = _worker_for(harness)
        asyncio.run(worker._drain_once())

        self.assertEqual(_completion_inserts(harness), [])
        self.assertEqual(sup.batch_completed, [("group:100", "B1")])

    def test_legacy_rows_without_batch_wake_directly(self) -> None:
        # 升级前落库的 tool_called 没有批次标记 → 维持旧行为：drain 后按 scope
        # 直接唤醒，不写 completion。
        rows = [_batched_row("EID1", "TC1", batch_id=None)]
        harness = _BatchHarness(pending_rows=rows, batch_status={})
        worker, sup = _worker_for(harness)
        asyncio.run(worker._drain_once())

        self.assertEqual(sup.woke, ["group:100"])
        self.assertEqual(sup.batch_completed, [])
        self.assertEqual(_completion_inserts(harness), [])


if __name__ == "__main__":
    unittest.main()

"""Contract tests for ToolWorker.

Covers (任务与决策契约 §5.1, §6, dispatcher 设计 2026-05-26):
- happy path: tool found, run returns dict → agent.tool_result written
  with tool_call_id / tool_name / task_id / result
- tool raises → agent.tool_failed with error_kind / error_message
- unknown tool_name → agent.tool_failed(unknown_tool)
- arguments 透传给 tool.run()
- self-wake: _drain_once 完成后对每个 scope 调一次 supervisor.wake()
  （拓扑 README §5.3 修复）

直接测 `_process_one()`，跳过 SQL SELECT 路径，session 用 _RecordingSession 捕获 inserts。
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from qqbot.services.agent_loop.delivery_claims import (
    DEFAULT_LEASE_SECONDS,
    ClaimResult,
)
from qqbot.services.agent_loop.tool_registry import ToolRegistry
from qqbot.services.agent_loop.tool_worker import ToolWorker


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


def _types(captured: list[Any]) -> list[str]:
    return [stmt.compile().params.get("type") for stmt in captured]


def _payloads_by_type(captured: list[Any]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for stmt in captured:
        params = stmt.compile().params
        out[params.get("type")] = params.get("payload") or {}
    return out


class _StubTool:
    def __init__(
        self,
        name: str,
        return_value: Any = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self.name = name
        self.description = "stub"
        self.arguments_schema = {"type": "object"}
        self._ret = return_value if return_value is not None else {"ok": True}
        self._raise = raise_exc
        self.calls: list[dict] = []

    async def run(self, arguments: dict, **_: Any) -> Any:
        # **_ 兼容 Tool 协议的 context kwargs（scope_key 等），与 WebsearchTool 等
        # 真实 tool 一致。
        self.calls.append(arguments)
        if self._raise:
            raise self._raise
        return self._ret


def _row(
    *,
    event_id: str = "EID",
    scope: str = "group",
    group_id: int | None = 100,
    user_id: int | None = None,
    correlation_id: str = "CID",
    payload: dict | None = None,
) -> dict:
    return {
        "event_id": event_id,
        "scope": scope,
        "group_id": group_id,
        "user_id": user_id,
        "correlation_id": correlation_id,
        "payload": payload or {},
    }


class ToolWorkerContractTest(unittest.TestCase):
    def test_happy_path_writes_tool_result(self) -> None:
        reg = ToolRegistry()
        tool = _StubTool(
            "websearch",
            return_value={"query": "x", "results": [{"url": "u"}]},
        )
        reg.register(tool)
        store: list[Any] = []
        worker = ToolWorker(session_factory=_factory_for(store), registry=reg)

        row = _row(
            event_id="EID1",
            payload={
                "tool_call_id": "TCID1",
                "tool_name": "websearch",
                "arguments": {"query": "x"},
                "task_id": "TASK1",
            },
        )
        asyncio.run(worker._process_one(row))

        # tool was invoked with the exact arguments
        self.assertEqual(tool.calls, [{"query": "x"}])

        types = _types(store)
        self.assertIn("agent.tool_result", types)
        self.assertNotIn("agent.tool_failed", types)

        result_payload = _payloads_by_type(store)["agent.tool_result"]
        self.assertEqual(result_payload["tool_call_id"], "TCID1")
        self.assertEqual(result_payload["tool_name"], "websearch")
        self.assertEqual(result_payload["task_id"], "TASK1")
        self.assertEqual(
            result_payload["result"],
            {"query": "x", "results": [{"url": "u"}]},
        )

    def test_tool_raises_writes_tool_failed(self) -> None:
        reg = ToolRegistry()
        reg.register(
            _StubTool("websearch", raise_exc=RuntimeError("upstream 500"))
        )
        store: list[Any] = []
        worker = ToolWorker(session_factory=_factory_for(store), registry=reg)

        row = _row(
            event_id="EID2",
            payload={
                "tool_call_id": "TCID2",
                "tool_name": "websearch",
                "arguments": {"query": "y"},
                "task_id": None,
            },
        )
        asyncio.run(worker._process_one(row))

        types = _types(store)
        self.assertIn("agent.tool_failed", types)
        self.assertNotIn("agent.tool_result", types)

        failed = _payloads_by_type(store)["agent.tool_failed"]
        self.assertEqual(failed["tool_call_id"], "TCID2")
        self.assertEqual(failed["tool_name"], "websearch")
        self.assertEqual(failed["error_kind"], "RuntimeError")
        self.assertIn("upstream 500", failed["error_message"])

    def test_unknown_tool_writes_failed_unknown_tool(self) -> None:
        reg = ToolRegistry()  # empty
        store: list[Any] = []
        worker = ToolWorker(session_factory=_factory_for(store), registry=reg)

        row = _row(
            event_id="EID3",
            payload={
                "tool_call_id": "TCID3",
                "tool_name": "no_such_tool",
                "arguments": {},
                "task_id": None,
            },
        )
        asyncio.run(worker._process_one(row))

        failed = _payloads_by_type(store)["agent.tool_failed"]
        self.assertEqual(failed["error_kind"], "unknown_tool")
        self.assertIn("no_such_tool", failed["error_message"])
        self.assertEqual(failed["tool_name"], "no_such_tool")

    def test_arguments_default_to_empty_dict(self) -> None:
        reg = ToolRegistry()
        tool = _StubTool("t1")
        reg.register(tool)
        store: list[Any] = []
        worker = ToolWorker(session_factory=_factory_for(store), registry=reg)

        row = _row(
            event_id="EID4",
            payload={"tool_call_id": "TCID4", "tool_name": "t1"},
        )
        asyncio.run(worker._process_one(row))
        self.assertEqual(tool.calls, [{}])
        self.assertIn("agent.tool_result", _types(store))

    def test_live_lease_skip_schedules_retry_without_running_tool(self) -> None:
        reg = ToolRegistry()
        tool = _StubTool("websearch")
        reg.register(tool)
        store: list[Any] = []
        worker = ToolWorker(session_factory=_factory_for(store), registry=reg)
        delays: list[float] = []
        worker._schedule_retry = delays.append  # type: ignore[method-assign]

        row = _row(
            event_id="EID5",
            payload={
                "tool_call_id": "TCID5",
                "tool_name": "websearch",
                "arguments": {"query": "later"},
            },
        )
        with patch(
            "qqbot.services.agent_loop.tool_worker.claim_delivery",
            new=AsyncMock(
                return_value=ClaimResult(
                    claimed=False, retry_after_seconds=5.0
                )
            ),
        ):
            result = asyncio.run(worker._process_one(row))

        self.assertIsNone(result)
        self.assertEqual(delays, [5.0])
        self.assertEqual(tool.calls, [])
        self.assertEqual(store, [])

    def test_terminal_write_failure_schedules_lease_retry(self) -> None:
        reg = ToolRegistry()
        tool = _StubTool("websearch", return_value={"ok": True})
        reg.register(tool)
        store: list[Any] = []
        worker = ToolWorker(session_factory=_factory_for(store), registry=reg)
        delays: list[float] = []
        worker._schedule_retry = delays.append  # type: ignore[method-assign]

        row = _row(
            event_id="EID6",
            payload={
                "tool_call_id": "TCID6",
                "tool_name": "websearch",
                "arguments": {"query": "x"},
            },
        )
        with patch(
            "qqbot.services.agent_loop.tool_worker.claim_delivery",
            new=AsyncMock(return_value=ClaimResult(claimed=True)),
        ), patch(
            "qqbot.services.agent_loop.tool_worker.write_agent_event",
            new=AsyncMock(side_effect=RuntimeError("db down")),
        ):
            with self.assertRaises(RuntimeError):
                asyncio.run(worker._process_one(row))

        self.assertEqual(tool.calls, [{"query": "x"}])
        self.assertEqual(delays, [float(DEFAULT_LEASE_SECONDS)])


class _StubSupervisor:
    """Captures wake() invocations for assertions; mirrors LoopSupervisor's
    async wake(scope_key) interface."""

    def __init__(self) -> None:
        self.woke: list[str] = []

    async def wake(self, scope_key: str) -> None:
        self.woke.append(scope_key)


class _SelectSession:
    """Single-shot session that answers the _PENDING_QUERY SELECT with a
    prepared row list, ignores everything else. Used as the FIRST session
    handed out by the factory in drain tests."""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    async def execute(self, stmt: Any) -> Any:
        class _R:
            def __init__(self, rs: list[dict]) -> None:
                self._rs = rs

            def mappings(self) -> "_R":
                return self

            def all(self) -> list[dict]:
                return self._rs

        return _R(self._rows)

    async def __aenter__(self) -> "_SelectSession":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None


def _drain_factory(store: list[Any], select_rows: list[dict]):
    """First session call returns the SELECT result; later calls (one per
    write_agent_event) record inserts into `store`."""
    state = {"calls": 0}

    def factory():
        state["calls"] += 1
        if state["calls"] == 1:
            return _SelectSession(select_rows)
        return _RecordingSession(store)

    return factory


class ToolWorkerSelfWakeTest(unittest.TestCase):
    def test_drain_wakes_supervisor_per_scope(self) -> None:
        reg = ToolRegistry()
        reg.register(_StubTool("websearch", return_value={"ok": True}))
        store: list[Any] = []
        supervisor = _StubSupervisor()

        rows = [
            _row(
                event_id="EID1",
                scope="group",
                group_id=100,
                payload={
                    "tool_call_id": "TC1",
                    "tool_name": "websearch",
                    "arguments": {"q": "a"},
                },
            ),
            _row(
                event_id="EID2",
                scope="group",
                group_id=100,
                payload={
                    "tool_call_id": "TC2",
                    "tool_name": "websearch",
                    "arguments": {"q": "b"},
                },
            ),
            _row(
                event_id="EID3",
                scope="group",
                group_id=200,
                payload={
                    "tool_call_id": "TC3",
                    "tool_name": "websearch",
                    "arguments": {"q": "c"},
                },
            ),
        ]
        worker = ToolWorker(
            session_factory=_drain_factory(store, rows),
            registry=reg,
            supervisor=supervisor,
        )

        processed = asyncio.run(worker._drain_once())
        self.assertEqual(processed, 3)
        # 同 scope 多条 result 合并成单次 wake；不同 scope 各 wake 一次
        self.assertEqual(sorted(supervisor.woke), ["group:100", "group:200"])

    def test_drain_without_supervisor_is_noop(self) -> None:
        reg = ToolRegistry()
        reg.register(_StubTool("t", return_value={"ok": True}))
        store: list[Any] = []

        rows = [
            _row(
                event_id="EID1",
                scope="group",
                group_id=100,
                payload={
                    "tool_call_id": "TC1",
                    "tool_name": "t",
                    "arguments": {},
                },
            ),
        ]
        # supervisor=None 走早期骨架兼容分支：不应抛错
        worker = ToolWorker(
            session_factory=_drain_factory(store, rows),
            registry=reg,
        )
        processed = asyncio.run(worker._drain_once())
        self.assertEqual(processed, 1)
        self.assertIn("agent.tool_result", _types(store))

    def test_process_one_returns_scope_key(self) -> None:
        """会落终态事件的分支需返回 scope_key，供 drain 层收集 wake 集合。"""
        reg = ToolRegistry()
        reg.register(_StubTool("ok", return_value={"r": 1}))
        reg.register(_StubTool("boom", raise_exc=RuntimeError("x")))
        store: list[Any] = []
        worker = ToolWorker(
            session_factory=_factory_for(store), registry=reg
        )

        scope_ok = asyncio.run(
            worker._process_one(
                _row(
                    scope="group",
                    group_id=100,
                    payload={"tool_call_id": "T1", "tool_name": "ok"},
                )
            )
        )
        scope_err = asyncio.run(
            worker._process_one(
                _row(
                    scope="group",
                    group_id=200,
                    payload={"tool_call_id": "T2", "tool_name": "boom"},
                )
            )
        )
        scope_unknown = asyncio.run(
            worker._process_one(
                _row(
                    scope="private",
                    group_id=None,
                    user_id=42,
                    payload={"tool_call_id": "T3", "tool_name": "nope"},
                )
            )
        )
        self.assertEqual(scope_ok, "group:100")
        self.assertEqual(scope_err, "group:200")
        self.assertEqual(scope_unknown, "private:42")


if __name__ == "__main__":
    unittest.main()

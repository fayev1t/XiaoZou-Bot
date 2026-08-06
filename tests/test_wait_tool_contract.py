"""Contract tests for WaitTool（模型时间自主权，2026-07-02）.

设计结论：
- execute() **绝不 sleep**：登记 asyncio 定时器后立刻返回成功（带 wake_at），
  当前拍的程序执行通道不被 sleep 占用。
- 到点回调 _fire_wait：**先**写 runtime.wait_elapsed（agent_visible，回显
  note），**再** wake(scope_key)——醒来那拍的投影必能看到 hint。
- 定时器仅存内存，进程重启即丢（best-effort，契约见 事件系统设计.md §4.3.2）。
- 参数越界 / 类型错 → invalid_arguments；wake_scope/session 未注入 →
  internal_tool_error（不 raise、不假装约上了）。
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from typing import Any

from qqbot.services.agent_loop.tools.wait import (
    MAX_WAIT_SECONDS,
    MIN_WAIT_SECONDS,
    WaitTool,
    _fire_wait,
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


def _values(stmt: Any) -> dict:
    return {k: v for k, v in stmt.compile().params.items()}


class WaitToolPromptContractTests(unittest.TestCase):
    def test_description_uses_append_only_reply_semantics(self) -> None:
        description = WaitTool.description
        self.assertIn("不读取或修改 reply 等待", description)
        self.assertIn("计时器仅保存在进程内存", description)

    def test_usage_prompt_uses_append_only_reply_semantics(self) -> None:
        usage_prompt = WaitTool.usage_prompt
        self.assertIn("相互独立", usage_prompt)
        self.assertIn("最新修订替换旧修订", usage_prompt)


class WaitToolScheduleTests(unittest.IsolatedAsyncioTestCase):
    def _context(self, timeline: list[Any]) -> dict:
        async def _wake(scope_key: str) -> None:
            timeline.append(("wake", scope_key))

        def factory() -> _RecordingSession:
            return _RecordingSession(timeline)

        return {
            "scope_key": "group:100",
            "session_factory": factory,
            "wake_scope": _wake,
            "correlation_id": "CID",
            "tool_call_event_id": "TC_EVENT",
        }

    async def test_schedule_returns_immediately_with_wake_at(self) -> None:
        timeline: list[Any] = []
        outcome = await WaitTool().run(
            {"seconds": 30, "note": "等小徐贴完日志"},
            **self._context(timeline),
        )
        self.assertTrue(outcome.ok)
        self.assertTrue(outcome.result["scheduled"])
        self.assertEqual(outcome.result["seconds"], 30)
        self.assertIn("wake_at", outcome.result)
        self.assertEqual(outcome.result["note"], "等小徐贴完日志")
        # 立刻返回：既没写事件也没唤醒（定时器还没到点）
        self.assertEqual(timeline, [])

    async def test_seconds_out_of_range_invalid_arguments(self) -> None:
        for bad in (MIN_WAIT_SECONDS - 1, MAX_WAIT_SECONDS + 1):
            outcome = await WaitTool().run(
                {"seconds": bad}, **self._context([])
            )
            self.assertFalse(outcome.ok)
            self.assertEqual(outcome.error_kind, "invalid_arguments")

    async def test_seconds_not_int_invalid_arguments(self) -> None:
        for bad in ("abc", None, 3.5, True):
            outcome = await WaitTool().run(
                {"seconds": bad}, **self._context([])
            )
            self.assertFalse(outcome.ok)
            self.assertEqual(outcome.error_kind, "invalid_arguments")

    async def test_numeric_string_seconds_accepted(self) -> None:
        outcome = await WaitTool().run(
            {"seconds": "60", "note": "一分钟后回来"}, **self._context([])
        )
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.result["seconds"], 60)

    async def test_missing_wake_scope_fails_gracefully(self) -> None:
        ctx = self._context([])
        ctx["wake_scope"] = None
        outcome = await WaitTool().run(
            {"seconds": 30, "note": "半分钟后回来"}, **ctx
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "internal_tool_error")


class WaitFireTests(unittest.IsolatedAsyncioTestCase):
    async def test_fire_writes_event_then_wakes_in_order(self) -> None:
        timeline: list[Any] = []

        async def _wake(scope_key: str) -> None:
            timeline.append(("wake", scope_key))

        def factory() -> _RecordingSession:
            return _RecordingSession(timeline)

        await _fire_wait(
            session_factory=factory,
            wake_scope=_wake,
            note_activity=None,
            scope_key="group:100",
            correlation_id="CID",
            causation_id="TC_EVENT",
            seconds=30,
            note="等小徐贴完日志",
            wake_at_iso="2026-07-02T16:00:00+08:00",
        )
        # 先事件后唤醒
        self.assertEqual(len(timeline), 2)
        stmt, wake_call = timeline
        values = _values(stmt)
        self.assertEqual(values["type"], "runtime.wait_elapsed")
        self.assertEqual(values["visibility"], "agent_visible")
        self.assertEqual(values["causation_id"], "TC_EVENT")
        self.assertEqual(values["payload"]["seconds"], 30)
        self.assertEqual(values["payload"]["note"], "等小徐贴完日志")
        self.assertEqual(wake_call, ("wake", "group:100"))

    async def test_fire_still_wakes_when_event_write_fails(self) -> None:
        woke: list[str] = []

        async def _wake(scope_key: str) -> None:
            woke.append(scope_key)

        class _BrokenSession(_RecordingSession):
            async def execute(self, stmt: Any) -> Any:
                raise RuntimeError("db down")

        def factory() -> _BrokenSession:
            return _BrokenSession([])

        await _fire_wait(
            session_factory=factory,
            wake_scope=_wake,
            note_activity=None,
            scope_key="group:100",
            correlation_id="CID",
            causation_id=None,
            seconds=30,
            note="事件写失败也要叫醒",
            wake_at_iso="2026-07-02T16:00:00+08:00",
        )
        # 事件写失败不吞唤醒——宁可少一条 hint，不可失约
        self.assertEqual(woke, ["group:100"])


class WaitNoteAndBoundsContractTests(unittest.IsolatedAsyncioTestCase):
    """2026-08-03：note 由可选改必填、上界 3600 → 6000（静默叫醒配套）。

    改动动机见 wait.py docstring —— 本工具现在同时承担"她给自己的回想改期"，
    而一次没有理由的自我约定到点只回显一个空提示。
    """

    async def _run(self, arguments: dict) -> Any:
        async def _wake(scope_key: str) -> None:
            return None

        def factory() -> _RecordingSession:
            return _RecordingSession([])

        return await WaitTool().run(
            arguments,
            scope_key="group:100",
            session_factory=factory,
            wake_scope=_wake,
            correlation_id="CID",
            tool_call_event_id="TC_EVENT",
        )

    def test_note_is_declared_required_in_arguments_schema(self) -> None:
        self.assertIn("note", WaitTool.arguments_schema["required"])
        self.assertIn("note", WaitTool.result_schema["required"])

    async def test_missing_note_is_invalid_arguments(self) -> None:
        outcome = await self._run({"seconds": 30})
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "invalid_arguments")
        self.assertEqual(outcome.extra.get("reason_code"), "note_not_str")

    async def test_blank_note_is_invalid_arguments(self) -> None:
        outcome = await self._run({"seconds": 30, "note": "   "})
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.extra.get("reason_code"), "note_empty")

    async def test_upper_bound_is_six_thousand_seconds(self) -> None:
        self.assertEqual(MAX_WAIT_SECONDS, 6000)
        ok = await self._run({"seconds": 6000, "note": "一小时四十分后再看"})
        self.assertTrue(ok.ok)
        too_long = await self._run({"seconds": 6001, "note": "超了"})
        self.assertFalse(too_long.ok)
        self.assertEqual(
            too_long.extra.get("reason_code"), "seconds_out_of_range"
        )


class WaitTimerIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_execute_timer_fires_end_to_end(self) -> None:
        """端到端：execute 登记的定时器到点后写事件 + 唤醒。
        MIN_WAIT_SECONDS 补丁到 0 让定时器立即到点，不拖慢套件。"""
        from unittest.mock import patch

        timeline: list[Any] = []

        async def _wake(scope_key: str) -> None:
            timeline.append(("wake", scope_key))

        def factory() -> _RecordingSession:
            return _RecordingSession(timeline)

        with patch(
            "qqbot.services.agent_loop.tools.wait.MIN_WAIT_SECONDS", 0
        ):
            outcome = await WaitTool().run(
                {"seconds": 0, "note": "立刻回来"},
                scope_key="group:100",
                session_factory=factory,
                wake_scope=_wake,
                correlation_id="CID",
                tool_call_event_id="TC_EVENT",
            )
        self.assertTrue(outcome.ok)
        await asyncio.sleep(0.1)  # 让 0 秒定时器与 _fire_wait 协程跑完

        stmts = [item for item in timeline if not isinstance(item, tuple)]
        self.assertEqual(len(stmts), 1)
        values = _values(stmts[0])
        self.assertEqual(values["type"], "runtime.wait_elapsed")
        self.assertEqual(values["payload"]["note"], "立刻回来")
        self.assertIn(("wake", "group:100"), timeline)


if __name__ == "__main__":
    unittest.main()

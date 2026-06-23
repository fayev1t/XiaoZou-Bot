"""Contract tests for SearchHistoryTool.

Covers:
- arguments 参数解析（task_id / anchor_event_id / start_time / end_time / query / limit）
- scope_key 上下文缺失 → raise ValueError（让 ToolWorker 写 tool_failed）
- task_id 解析为 triggered_by_event_id 锚点；查不到 → warning，不报错
- limit 兜底（默认 / 上限）
- 返回结构复用 Projector 渲染器，items 字段同构

不打真实 DB：直接 stub _query / _resolve_task_anchor 方法，验证调用面。
"""

from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from qqbot.services.agent_loop.projection import _EventSnapshot
from qqbot.services.agent_loop.tools.search_history import SearchHistoryTool


SHANGHAI = ZoneInfo("Asia/Shanghai")
BASE_TIME = datetime(2026, 5, 26, 14, 30, 0, tzinfo=SHANGHAI)


class _StubRow:
    """模拟 AgentEvent ORM row 的最小子集，supply _snapshot_from_row 用到的字段。"""

    def __init__(self, snap: _EventSnapshot) -> None:
        self.event_id = snap.event_id
        self.occurred_at = snap.occurred_at
        self.origin = snap.origin
        self.type = snap.type
        self.scope = snap.scope
        self.group_id = snap.group_id
        self.user_id = snap.user_id
        self.visibility = snap.visibility
        self.correlation_id = snap.correlation_id
        self.causation_id = snap.causation_id
        self.payload = snap.payload


def _msg(text: str, *, seconds_offset: int = 0, event_id: str = "MSG") -> _StubRow:
    snap = _EventSnapshot(
        event_id=event_id,
        occurred_at=BASE_TIME + timedelta(seconds=seconds_offset),
        origin="external",
        type="external.message.group.normal",
        scope="group",
        group_id=999,
        user_id=222,
        visibility="agent_visible",
        correlation_id=None,
        causation_id=None,
        payload={
            "raw_message": text,
            "segments": [{"type": "text", "data": {"text": text}}],
            "sender": {"nickname": "alice", "user_id": 222},
        },
    )
    return _StubRow(snap)


class SearchHistoryToolContractTest(unittest.TestCase):
    def _make_tool(
        self,
        *,
        query_returns: list[_StubRow] | None = None,
        anchor_returns: str | None = None,
    ) -> SearchHistoryTool:
        """构造工具，替换 _query 与 _resolve_task_anchor 为 stub。"""
        # 无构造依赖；session_factory 现从 run() context 进，且这些用例都 stub
        # 掉了 _query / _resolve_task_anchor，session_factory 不会被走到。
        tool = SearchHistoryTool()
        self.captured_query_kwargs: dict[str, Any] = {}

        async def _stub_query(**kwargs: Any) -> list[_StubRow]:
            self.captured_query_kwargs = kwargs
            return query_returns or []

        async def _stub_anchor(task_id: str) -> str | None:
            self.captured_resolve_task_id = task_id
            return anchor_returns

        tool._query = _stub_query  # type: ignore[method-assign]
        tool._resolve_task_anchor = _stub_anchor  # type: ignore[method-assign]
        return tool

    def test_scope_key_missing_raises(self) -> None:
        tool = self._make_tool()
        with self.assertRaises(ValueError):
            asyncio.run(tool.run({"limit": 5}))  # 没传 scope_key

    def test_invalid_scope_key_raises(self) -> None:
        tool = self._make_tool()
        with self.assertRaises(ValueError):
            asyncio.run(tool.run({}, scope_key="bogus:1"))

    def test_happy_path_returns_rendered_items(self) -> None:
        rows = [_msg("hello world", seconds_offset=i, event_id=f"E{i:02d}") for i in range(3)]
        tool = self._make_tool(query_returns=rows)
        result = asyncio.run(tool.run({"limit": 10}, scope_key="group:999"))
        self.assertEqual(result["matched"], 3)
        self.assertEqual(len(result["items"]), 3)
        # 渲染走 Projector：必带 sender + text
        for item in result["items"]:
            self.assertEqual(item["kind"], "message")
            self.assertIn("alice(222)", item["render"])
            self.assertIn("hello world", item["render"])

    def test_limit_clamped_to_max(self) -> None:
        tool = self._make_tool()
        asyncio.run(tool.run({"limit": 9999}, scope_key="group:999"))
        # _query 收到的 limit 不超过工具的 _MAX_LIMIT (50)
        self.assertEqual(self.captured_query_kwargs["limit"], 50)

    def test_limit_defaults_when_invalid(self) -> None:
        tool = self._make_tool()
        asyncio.run(tool.run({"limit": "not-a-number"}, scope_key="group:999"))
        self.assertEqual(self.captured_query_kwargs["limit"], 20)  # _DEFAULT_LIMIT

    def test_limit_min_clamped_to_one(self) -> None:
        tool = self._make_tool()
        asyncio.run(tool.run({"limit": 0}, scope_key="group:999"))
        self.assertEqual(self.captured_query_kwargs["limit"], 1)

    def test_anchor_event_id_passed_through(self) -> None:
        tool = self._make_tool()
        asyncio.run(
            tool.run(
                {"anchor_event_id": "ANCHOR123"},
                scope_key="group:999",
            )
        )
        self.assertEqual(self.captured_query_kwargs["anchor_event_id"], "ANCHOR123")

    def test_task_id_resolved_to_anchor(self) -> None:
        tool = self._make_tool(anchor_returns="RESOLVED_ANCHOR")
        result = asyncio.run(
            tool.run({"task_id": "T1"}, scope_key="group:999")
        )
        self.assertEqual(self.captured_query_kwargs["anchor_event_id"], "RESOLVED_ANCHOR")
        self.assertEqual(result["anchor_event_id"], "RESOLVED_ANCHOR")
        # 无 warning：解析成功
        self.assertEqual(result["warnings"], [])

    def test_task_id_with_no_anchor_warns(self) -> None:
        tool = self._make_tool(anchor_returns=None)
        result = asyncio.run(
            tool.run({"task_id": "T_MISSING"}, scope_key="group:999")
        )
        self.assertIsNone(self.captured_query_kwargs["anchor_event_id"])
        self.assertTrue(any("T_MISSING" in w for w in result["warnings"]))

    def test_explicit_anchor_wins_over_task_id(self) -> None:
        tool = self._make_tool(anchor_returns="FROM_TASK")
        asyncio.run(
            tool.run(
                {"anchor_event_id": "EXPLICIT", "task_id": "T1"},
                scope_key="group:999",
            )
        )
        # 显式 anchor 优先，不会回去查 task
        self.assertEqual(self.captured_query_kwargs["anchor_event_id"], "EXPLICIT")

    def test_time_window_parsed_to_datetimes(self) -> None:
        tool = self._make_tool()
        asyncio.run(
            tool.run(
                {
                    "start_time": "2026-05-25T00:00:00+08:00",
                    "end_time": "2026-05-26T00:00:00+08:00",
                },
                scope_key="group:999",
            )
        )
        start = self.captured_query_kwargs["start_dt"]
        end = self.captured_query_kwargs["end_dt"]
        self.assertIsNotNone(start)
        self.assertIsNotNone(end)
        self.assertLess(start, end)

    def test_unparseable_time_yields_warning(self) -> None:
        tool = self._make_tool()
        result = asyncio.run(
            tool.run({"start_time": "not-a-time"}, scope_key="group:999")
        )
        self.assertIsNone(self.captured_query_kwargs["start_dt"])
        self.assertTrue(any("not-a-time" in w for w in result["warnings"]))

    def test_query_passed_through(self) -> None:
        tool = self._make_tool()
        asyncio.run(
            tool.run({"query": "5432 error"}, scope_key="group:999")
        )
        self.assertEqual(self.captured_query_kwargs["query"], "5432 error")

    def test_blank_arguments_yield_empty_strings_as_none(self) -> None:
        tool = self._make_tool()
        asyncio.run(
            tool.run(
                {"query": "   ", "anchor_event_id": ""},
                scope_key="group:999",
            )
        )
        self.assertIsNone(self.captured_query_kwargs["query"])
        self.assertIsNone(self.captured_query_kwargs["anchor_event_id"])

    def test_scope_filter_propagates_to_query(self) -> None:
        tool = self._make_tool()
        asyncio.run(tool.run({}, scope_key="group:42"))
        self.assertEqual(self.captured_query_kwargs["scope"], "group")
        self.assertEqual(self.captured_query_kwargs["group_id"], 42)


if __name__ == "__main__":
    unittest.main()

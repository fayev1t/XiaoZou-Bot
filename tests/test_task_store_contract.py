"""Contract tests for the agent_tasks read model (task_store).

Covers (③状态折叠与投影 §6.1 选项A —— 任务持久化读模型):
- apply_task_event 三类事件分派：task_created→INSERT agent_tasks，
  task_state_changed→UPDATE，task_progress_noted→UPDATE（JSONB 追加）；
  缺 task_id / to_state / note → no-op（与 fold_tasks 宽容语义一致）
- _row_to_task_view 纯转换：progress_notes 取尾部 N 条、时间 normalize 到北京、
  pending_tool_call_ids 恒空（在途调用交给窗口折叠）
- load_active_tasks：只取 pending/running，按 created_at 升序还原 TaskView
- Projector.merge_active_tasks 纯合并：窗口版优先、读模型补窗口外缺口、
  按 created_at 升序、无补充时原样返回（identity）

不打真实 DB：apply_task_event 用 recording session 捕获语句；load_active_tasks
用返回固定行的 stub session。语句按 postgresql dialect 编译以支持 JSONB。
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.dml import Insert, Update

from qqbot.services.agent_loop import task_store
from qqbot.services.agent_loop.decision import TaskView
from qqbot.services.agent_loop.projection import Projector
from qqbot.services.agent_loop.task_store import (
    apply_task_event,
    load_active_tasks,
    _row_to_task_view,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
BASE = datetime(2026, 6, 1, 12, 0, 0, tzinfo=SHANGHAI)


# ─── fakes ───


class _RecordingSession:
    """捕获 execute 的语句，不落库。apply_task_event 不 commit（调用方负责）。"""

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


class _ScalarsResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> "_ScalarsResult":
        return self

    def all(self) -> list[Any]:
        return self._rows


class _ReadSession:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows
        self.captured: Any = None

    async def execute(self, stmt: Any) -> _ScalarsResult:
        self.captured = stmt
        return _ScalarsResult(self._rows)

    async def commit(self) -> None:
        return None

    async def __aenter__(self) -> "_ReadSession":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None


def _task_row(
    *,
    task_id: str,
    scope_key: str = "group:1",
    state: str = "running",
    description: str = "盯周报",
    created_at: datetime | None = None,
    progress_notes: list[dict] | None = None,
) -> SimpleNamespace:
    """伪 AgentTask ORM row（_row_to_task_view / load 只读这些属性）。"""
    return SimpleNamespace(
        task_id=task_id,
        scope_key=scope_key,
        description=description,
        related_tools=["websearch"],
        parent_task_id=None,
        state=state,
        created_at=created_at or BASE,
        last_changed_at=created_at or BASE,
        last_change_reason=None,
        triggered_by_event_id="EVT_ANCHOR",
        progress_notes=progress_notes or [],
    )


def _params(stmt: Any) -> dict:
    return stmt.compile(dialect=postgresql.dialect()).params


def _table_name(stmt: Any) -> str:
    return stmt.table.name


# ─── apply_task_event 分派 ───


class ApplyTaskEventTests(unittest.IsolatedAsyncioTestCase):
    async def _apply(self, **kw: Any) -> list[Any]:
        store: list[Any] = []
        await apply_task_event(_RecordingSession(store), **kw)
        return store

    async def test_task_created_inserts_into_agent_tasks(self) -> None:
        store = await self._apply(
            event_type="agent.task_created",
            scope_key="group:1",
            occurred_at=BASE,
            payload={
                "task_id": "T1",
                "description": "盯周报",
                "related_tools": ["websearch"],
                "parent_task_id": None,
                "triggered_by_event_id": "EVT1",
            },
        )
        self.assertEqual(len(store), 1)
        stmt = store[0]
        self.assertIsInstance(stmt, Insert)
        self.assertEqual(_table_name(stmt), "agent_tasks")
        params = _params(stmt)
        self.assertEqual(params["task_id"], "T1")
        self.assertEqual(params["scope_key"], "group:1")
        self.assertEqual(params["state"], "pending")

    async def test_state_changed_updates(self) -> None:
        store = await self._apply(
            event_type="agent.task_state_changed",
            scope_key="group:1",
            occurred_at=BASE,
            payload={"task_id": "T1", "to_state": "done", "reason": "ok"},
        )
        self.assertEqual(len(store), 1)
        stmt = store[0]
        self.assertIsInstance(stmt, Update)
        self.assertEqual(_table_name(stmt), "agent_tasks")
        self.assertEqual(_params(stmt)["state"], "done")

    async def test_progress_noted_updates(self) -> None:
        store = await self._apply(
            event_type="agent.task_progress_noted",
            scope_key="group:1",
            occurred_at=BASE,
            payload={"task_id": "T1", "note": "已查到周报模板"},
        )
        self.assertEqual(len(store), 1)
        self.assertIsInstance(store[0], Update)
        self.assertEqual(_table_name(store[0]), "agent_tasks")

    async def test_missing_task_id_is_noop(self) -> None:
        store = await self._apply(
            event_type="agent.task_created",
            scope_key="group:1",
            occurred_at=BASE,
            payload={"description": "x"},
        )
        self.assertEqual(store, [])

    async def test_state_changed_without_to_state_is_noop(self) -> None:
        store = await self._apply(
            event_type="agent.task_state_changed",
            scope_key="group:1",
            occurred_at=BASE,
            payload={"task_id": "T1", "reason": "x"},
        )
        self.assertEqual(store, [])

    async def test_unknown_task_event_is_noop(self) -> None:
        store = await self._apply(
            event_type="agent.task_archived",  # 前向兼容：未知 task_* 不炸
            scope_key="group:1",
            occurred_at=BASE,
            payload={"task_id": "T1"},
        )
        self.assertEqual(store, [])


# ─── _row_to_task_view 纯转换 ───


class RowToTaskViewTests(unittest.TestCase):
    def test_basic_fields(self) -> None:
        view = _row_to_task_view(_task_row(task_id="T1"))
        self.assertIsInstance(view, TaskView)
        self.assertEqual(view.task_id, "T1")
        self.assertEqual(view.scope_key, "group:1")
        self.assertEqual(view.state, "running")
        self.assertEqual(view.triggered_by_event_id, "EVT_ANCHOR")
        # 在途工具调用读模型不维护
        self.assertEqual(view.pending_tool_call_ids, [])

    def test_progress_notes_capped_to_last_n(self) -> None:
        notes = [
            {"at": (BASE + timedelta(minutes=i)).isoformat(), "note": f"n{i}"}
            for i in range(7)
        ]
        view = _row_to_task_view(_task_row(task_id="T1", progress_notes=notes))
        self.assertEqual(len(view.progress_notes), task_store.MAX_PROGRESS_NOTES)
        # 保留的是尾部 N 条
        self.assertEqual(view.progress_notes[-1].note, "n6")
        self.assertEqual(view.progress_notes[0].note, "n2")

    def test_naive_or_utc_time_normalized_to_china(self) -> None:
        utc_dt = datetime(2026, 6, 1, 4, 0, 0, tzinfo=timezone.utc)  # = 12:00 +08
        view = _row_to_task_view(_task_row(task_id="T1", created_at=utc_dt))
        self.assertEqual(view.created_at.utcoffset(), timedelta(hours=8))
        self.assertEqual(view.created_at.hour, 12)

    def test_malformed_progress_note_skipped(self) -> None:
        notes = [{"note": "no-at"}, "not-a-dict", {"at": BASE.isoformat(), "note": "ok"}]
        view = _row_to_task_view(_task_row(task_id="T1", progress_notes=notes))
        self.assertEqual([n.note for n in view.progress_notes], ["ok"])


# ─── load_active_tasks ───


class LoadActiveTasksTests(unittest.IsolatedAsyncioTestCase):
    async def test_rows_become_task_views(self) -> None:
        rows = [
            _task_row(task_id="T1", created_at=BASE),
            _task_row(task_id="T2", created_at=BASE + timedelta(minutes=5)),
        ]
        session = _ReadSession(rows)

        def factory() -> _ReadSession:
            return session

        views = await load_active_tasks(factory, "group:1")
        self.assertEqual([v.task_id for v in views], ["T1", "T2"])
        self.assertTrue(all(isinstance(v, TaskView) for v in views))


# ─── Projector.merge_active_tasks 纯合并 ───


def _view(
    task_id: str,
    created_at: datetime,
    state: str = "running",
    pending_tool_call_ids: list[str] | None = None,
) -> TaskView:
    return TaskView(
        task_id=task_id,
        scope_key="group:1",
        description=task_id,
        related_tools=[],
        parent_task_id=None,
        state=state,  # type: ignore[arg-type]
        created_at=created_at,
        last_changed_at=created_at,
        last_change_reason=None,
        pending_tool_call_ids=pending_tool_call_ids or [],
    )


class MergeActiveTasksTests(unittest.TestCase):
    def test_persisted_fills_gap_and_sorted(self) -> None:
        window = [_view("T2", BASE + timedelta(minutes=5))]
        persisted = [
            _view("T1", BASE),  # 窗口外的旧任务 —— 应补回
            _view("T2", BASE + timedelta(minutes=5)),  # 与窗口重复 —— 不重复加
        ]
        merged = Projector.merge_active_tasks(window, persisted)
        self.assertEqual([v.task_id for v in merged], ["T1", "T2"])

    def test_window_version_wins_on_conflict(self) -> None:
        # 同 id 两边都有：保留窗口版（带在途 tool_call_ids；读模型版恒空）
        win = _view("T1", BASE, pending_tool_call_ids=["TC1"])
        merged = Projector.merge_active_tasks([win], [_view("T1", BASE)])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].pending_tool_call_ids, ["TC1"])

    def test_no_extra_returns_same_object(self) -> None:
        window = [_view("T1", BASE)]
        merged = Projector.merge_active_tasks(window, [])
        self.assertIs(merged, window)

    def test_empty_window_uses_persisted(self) -> None:
        persisted = [_view("T1", BASE)]
        merged = Projector.merge_active_tasks([], persisted)
        self.assertEqual([v.task_id for v in merged], ["T1"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

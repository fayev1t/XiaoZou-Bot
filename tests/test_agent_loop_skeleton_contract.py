"""Contract for the v2 AgentLoop skeleton (LoopSupervisor + AgentLoop + planner).

Pure unit-level; DB is faked by a recording session, no nonebot needed.

Verifies the skeleton produces the expected sequence of internal events on
one tick:
  runtime.tick_started → agent.decision_emitted → agent.idle_decision
  → runtime.tick_ended
all sharing the same correlation_id.

Also verifies:
- LoopSupervisor lazy-instantiates GroupAgentLoop on wake.
- LoopSupervisor silently drops private:* wakes.
- LoopSupervisor.start() spawns the system loop up front.
- EventIngest.wake is dispatched to supervisor on inserted external events
  and not dispatched for private / no-mapper events.
- scope_key parser handles all three scopes.
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from qqbot.services.agent_loop import (
    AgentLoop,
    FakeIdlePlanner,
    LoopSupervisor,
)
from qqbot.services.agent_loop.event_writer import parse_scope_key
from qqbot.services.event_ingest.ingest import _scope_key_for_wake
from qqbot.services.event_ingest.system_event import SystemEvent
from datetime import datetime
from zoneinfo import ZoneInfo


class _EmptyResult:
    """Mappings-compatible empty result for SELECT statements driven by
    ReplySendWorker's catchup query — keeps tests DB-free."""

    def mappings(self) -> "_EmptyResult":
        return self

    def all(self) -> list:
        return []


class _RecordingSession:
    """async session double that captures every executed insert statement.

    Reads (sqlalchemy.text(...) clauses, e.g. the ReplySendWorker catchup
    SELECT scheduled by LoopSupervisor.start()) are ignored by the recorder
    and return an empty mappings result. Only mutating statements (inserts
    via pg_insert) are appended to `store`.
    """

    def __init__(self, store: list[Any]) -> None:
        self._store = store

    async def execute(self, stmt: Any) -> Any:
        from sqlalchemy.sql.elements import TextClause

        if isinstance(stmt, TextClause):
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
    """Pull the column→value map out of a SQLAlchemy insert statement."""
    # pg_insert(...).values(...) builds a dict; SQLAlchemy stores it on
    # stmt.parameters or .compile().params depending on construction. We
    # use the .compile() route to keep it dialect-agnostic.
    return {k: v for k, v in stmt.compile().params.items()}


class ScopeKeyParserTests(unittest.TestCase):
    def test_system(self) -> None:
        self.assertEqual(parse_scope_key("system"), ("system", None, None))

    def test_group(self) -> None:
        self.assertEqual(parse_scope_key("group:12345"), ("group", 12345, None))

    def test_private(self) -> None:
        self.assertEqual(parse_scope_key("private:99"), ("private", None, 99))

    def test_invalid(self) -> None:
        with self.assertRaises(ValueError):
            parse_scope_key("bogus")


class IngestScopeRoutingTests(unittest.TestCase):
    def _ev(self, scope: str, group_id: int | None = None, user_id: int | None = None):
        return SystemEvent(
            event_id="x",
            occurred_at=datetime.now(ZoneInfo("Asia/Shanghai")),
            origin="external",
            type="t",
            scope=scope,
            group_id=group_id,
            user_id=user_id,
            visibility="agent_visible",
            correlation_id="x",
            causation_id=None,
            idempotency_key=None,
            payload={},
            raw=None,
        )

    def test_group_event_routes_to_group_scope_key(self) -> None:
        self.assertEqual(
            _scope_key_for_wake(self._ev("group", group_id=12345)), "group:12345"
        )

    def test_system_event_routes_to_system_scope_key(self) -> None:
        self.assertEqual(_scope_key_for_wake(self._ev("system")), "system")

    def test_private_event_does_not_wake(self) -> None:
        self.assertIsNone(_scope_key_for_wake(self._ev("private", user_id=222)))

    def test_group_event_without_group_id_does_not_wake(self) -> None:
        self.assertIsNone(_scope_key_for_wake(self._ev("group", group_id=None)))


class AgentLoopSkeletonTickTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_wake_produces_idle_tick_event_chain(self) -> None:
        captured: list[Any] = []
        loop = AgentLoop(
            scope_key="group:12345",
            planner=FakeIdlePlanner(),
            session_factory=_factory_for(captured),
        )
        loop.start()
        loop.wake()
        # 给 tick 一点时间跑完
        for _ in range(50):
            await asyncio.sleep(0.01)
            if len(captured) >= 4:
                break
        await loop.stop()

        # 期望事件序列：tick_started, decision_emitted, idle_decision, tick_ended
        types = [_values_of(stmt).get("type") for stmt in captured]
        self.assertEqual(
            types,
            [
                "runtime.tick_started",
                "agent.decision_emitted",
                "agent.idle_decision",
                "runtime.tick_ended",
            ],
        )

        # 同一 tick 内 correlation_id 一致
        corrs = {_values_of(stmt).get("correlation_id") for stmt in captured}
        self.assertEqual(len(corrs), 1)

        # decision_emitted → idle_decision 因果链
        decision_id = _values_of(captured[1]).get("event_id")
        idle_caus = _values_of(captured[2]).get("causation_id")
        self.assertEqual(idle_caus, decision_id)

        # tick_started → tick_ended 因果链
        tick_started_id = _values_of(captured[0]).get("event_id")
        tick_ended_caus = _values_of(captured[3]).get("causation_id")
        self.assertEqual(tick_ended_caus, tick_started_id)

    async def test_loop_idle_when_not_waked(self) -> None:
        captured: list[Any] = []
        loop = AgentLoop(
            scope_key="group:1",
            planner=FakeIdlePlanner(),
            session_factory=_factory_for(captured),
        )
        loop.start()
        await asyncio.sleep(0.05)
        await loop.stop()
        self.assertEqual(captured, [])

    async def test_bot_user_id_resolver_called_each_tick(self) -> None:
        """resolver 每 tick 被调一次 —— bot 重连后 self_id 可能变；每 tick
        重新 resolve 比启动期 snapshot 更稳。"""
        captured: list[Any] = []
        call_count = {"n": 0}

        def _resolver() -> str | None:
            call_count["n"] += 1
            return "3167291813"

        loop = AgentLoop(
            scope_key="group:12345",
            planner=FakeIdlePlanner(),
            session_factory=_factory_for(captured),
            bot_user_id_resolver=_resolver,
        )
        loop.start()
        loop.wake()
        for _ in range(50):
            await asyncio.sleep(0.01)
            if call_count["n"] >= 1:
                break
        await loop.stop()
        # 至少跑了一 tick → resolver 至少被调一次
        self.assertGreaterEqual(call_count["n"], 1)

    async def test_bot_user_id_resolver_exception_does_not_break_tick(self) -> None:
        """resolver 抛异常时整 tick 不应翻车 —— prompt 降级为没有 bot_user_id
        属性，业务继续。"""
        captured: list[Any] = []

        def _broken_resolver() -> str | None:
            raise RuntimeError("bot_registry unavailable")

        loop = AgentLoop(
            scope_key="group:12345",
            planner=FakeIdlePlanner(),
            session_factory=_factory_for(captured),
            bot_user_id_resolver=_broken_resolver,
        )
        loop.start()
        loop.wake()
        for _ in range(50):
            await asyncio.sleep(0.01)
            if len(captured) >= 4:
                break
        await loop.stop()
        # 正常 4 条事件链都应当落地（tick_started / decision_emitted /
        # idle_decision / tick_ended），不被 resolver 异常掐断
        types = [_values_of(stmt).get("type") for stmt in captured]
        self.assertIn("runtime.tick_started", types)
        self.assertIn("runtime.tick_ended", types)


class LoopSupervisorContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_spawns_system_loop(self) -> None:
        captured: list[Any] = []
        sup = LoopSupervisor(
            planner=FakeIdlePlanner(),
            session_factory=_factory_for(captured),
        )
        await sup.start()
        self.assertTrue(sup.started)
        self.assertEqual(sup.loop_count, 1)
        await sup.stop()

    async def test_wake_lazy_creates_group_loop(self) -> None:
        captured: list[Any] = []
        sup = LoopSupervisor(
            planner=FakeIdlePlanner(),
            session_factory=_factory_for(captured),
        )
        await sup.start()
        await sup.wake("group:12345")
        # 等 tick 落库
        for _ in range(50):
            await asyncio.sleep(0.01)
            if captured:
                break

        # 必须在 stop() 之前断言：stop 会 _loops.clear()，loop_count 归零。
        self.assertEqual(sup.loop_count, 2)  # system + group:12345
        # 至少有一个事件来自 group:12345
        group_ids = {_values_of(stmt).get("group_id") for stmt in captured}
        self.assertIn(12345, group_ids)

        await sup.stop()

    async def test_private_wake_is_silently_dropped(self) -> None:
        captured: list[Any] = []
        sup = LoopSupervisor(
            planner=FakeIdlePlanner(),
            session_factory=_factory_for(captured),
        )
        await sup.start()
        await sup.wake("private:222")
        await asyncio.sleep(0.02)

        # 同上：先断言再 stop。private wake 不应实例化 loop。
        self.assertEqual(sup.loop_count, 1)  # 只有 system
        # 没有 private 事件
        scopes = {_values_of(stmt).get("scope") for stmt in captured}
        self.assertNotIn("private", scopes)

        await sup.stop()

    async def test_wake_after_stop_is_noop(self) -> None:
        captured: list[Any] = []
        sup = LoopSupervisor(
            planner=FakeIdlePlanner(),
            session_factory=_factory_for(captured),
        )
        await sup.start()
        await sup.stop()
        # start() 期间任务读模型回填（task_store.backfill_recent）本身就会
        # 发一条 SELECT——本用例的断言对象是"stop 后 wake 不再产生任何新
        # 语句"，故取 stop 后的基线数比对，而非要求 captured 全程为空。
        baseline = len(captured)
        await sup.wake("group:1")
        await asyncio.sleep(0.02)
        self.assertEqual(len(captured), baseline)


class FakeIdlePlannerTests(unittest.IsolatedAsyncioTestCase):
    async def test_always_idle(self) -> None:
        from qqbot.services.agent_loop.decision import DecisionContext, IdleAction

        planner = FakeIdlePlanner()
        ctx = DecisionContext(
            scope_key="group:1",
            correlation_id="c",
            tick_seq=1,
            now=datetime.now(ZoneInfo("Asia/Shanghai")),
        )
        decision = await planner.decide(ctx)
        self.assertEqual(len(decision.actions), 1)
        self.assertIsInstance(decision.actions[0], IdleAction)
        self.assertEqual(decision.actions[0].reason, "bootstrap_skeleton")


class IngestSupervisorIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_ingest_calls_supervisor_wake_on_insert(self) -> None:
        from qqbot.services.event_ingest import EventIngest
        from qqbot.services.event_ingest.mappers import build_default_registry

        wake_calls: list[str] = []

        class FakeSupervisor:
            async def wake(self, scope_key: str) -> None:
                wake_calls.append(scope_key)

        class FakeSession:
            async def execute(self, stmt: Any) -> Any:
                return SimpleNamespace(rowcount=1)

            async def commit(self) -> None:
                return None

            async def __aenter__(self) -> "FakeSession":
                return self

            async def __aexit__(self, *args: Any) -> None:
                return None

        ingest = EventIngest(
            build_default_registry(),
            session_factory=FakeSession,
            supervisor=FakeSupervisor(),
        )
        # group message → wake group:999
        event = SimpleNamespace(
            post_type="message",
            message_type="group",
            sub_type="normal",
            time=1716700000,
            self_id=10000,
            message_id=12345,
            group_id=999,
            user_id=222,
            raw_message="hi",
            message=[],
            sender=None,
        )
        result = await ingest.ingest(event)
        self.assertEqual(result.status, "inserted")
        self.assertEqual(wake_calls, ["group:999"])

    async def test_ingest_does_not_wake_for_private_message(self) -> None:
        from qqbot.services.event_ingest import EventIngest
        from qqbot.services.event_ingest.mappers import build_default_registry

        wake_calls: list[str] = []

        class FakeSupervisor:
            async def wake(self, scope_key: str) -> None:
                wake_calls.append(scope_key)

        class FakeSession:
            async def execute(self, stmt: Any) -> Any:
                return SimpleNamespace(rowcount=1)

            async def commit(self) -> None:
                return None

            async def __aenter__(self) -> "FakeSession":
                return self

            async def __aexit__(self, *args: Any) -> None:
                return None

        ingest = EventIngest(
            build_default_registry(),
            session_factory=FakeSession,
            supervisor=FakeSupervisor(),
        )
        event = SimpleNamespace(
            post_type="message",
            message_type="private",
            sub_type="friend",
            time=1716700000,
            self_id=10000,
            message_id=5,
            user_id=222,
            raw_message="hi",
            message=[],
            sender=None,
        )
        result = await ingest.ingest(event)
        self.assertEqual(result.status, "inserted")
        self.assertEqual(wake_calls, [])  # private 不唤醒

    async def test_ingest_does_not_wake_for_duplicate(self) -> None:
        from qqbot.services.event_ingest import EventIngest
        from qqbot.services.event_ingest.mappers import build_default_registry

        wake_calls: list[str] = []

        class FakeSupervisor:
            async def wake(self, scope_key: str) -> None:
                wake_calls.append(scope_key)

        class FakeSession:
            async def execute(self, stmt: Any) -> Any:
                return SimpleNamespace(rowcount=0)  # conflict

            async def commit(self) -> None:
                return None

            async def __aenter__(self) -> "FakeSession":
                return self

            async def __aexit__(self, *args: Any) -> None:
                return None

        ingest = EventIngest(
            build_default_registry(),
            session_factory=FakeSession,
            supervisor=FakeSupervisor(),
        )
        event = SimpleNamespace(
            post_type="message", message_type="group", sub_type="normal",
            time=1716700000, self_id=10000, message_id=12345,
            group_id=999, user_id=222, raw_message="", message=[], sender=None,
        )
        result = await ingest.ingest(event)
        self.assertEqual(result.status, "duplicate")
        self.assertEqual(wake_calls, [])


if __name__ == "__main__":
    unittest.main()

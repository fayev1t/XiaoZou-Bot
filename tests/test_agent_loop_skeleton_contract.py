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
- 唤醒攒批窗口（2026-07-28）：默认 wake() 等安静后才开拍，一串唤醒并成一拍，
  immediate=True 绕过窗口，持续刷屏被 max delay 兜住。
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

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


class _SlowIdlePlanner:
    """模拟 LLM 往返：decide() 里睡一段可观测的时间再返回 idle。

    用来把"投影时刻"和"决策写入时刻"拉开到断言可分辨的距离。
    """

    DELAY = 0.15

    async def decide(self, context: Any) -> Any:
        from qqbot.services.agent_loop import DecisionOutput, IdleAction

        _ = context
        await asyncio.sleep(self.DELAY)
        return DecisionOutput(actions=[IdleAction(reason="slow-planner")])


class _SlowCallToolPlanner:
    """模拟 LLM 往返后产出动作的拍：睡完 DELAY 返回 create_task + call_tool。

    用来断言 _apply_actions 派生的动作事件（task_created / tool_called /
    自动推进的 task_state_changed）与 decision_emitted 同步回填投影时刻。
    """

    DELAY = 0.15

    async def decide(self, context: Any) -> Any:
        from qqbot.services.agent_loop import (
            CallToolAction,
            CreateTaskAction,
            DecisionOutput,
        )

        _ = context
        await asyncio.sleep(self.DELAY)
        return DecisionOutput(
            actions=[
                CreateTaskAction(description="慢拍任务", task_ref="ref-1"),
                CallToolAction(
                    tool_name="reply",
                    arguments={"brief": "x", "hold_seconds": 0},
                    task_ref="ref-1",
                ),
            ]
        )


class AgentLoopSkeletonTickTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_wake_produces_idle_tick_event_chain(self) -> None:
        captured: list[Any] = []
        loop = AgentLoop(
            scope_key="group:12345",
            planner=FakeIdlePlanner(),
            session_factory=_factory_for(captured),
        )
        loop.start()
        loop.wake(immediate=True)
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

    async def test_decision_timestamp_is_tick_start_not_write_time(self) -> None:
        """agent.decision_emitted.occurred_at = 本拍**投影时刻**，不是写入时刻
        （2026-07-24，待办清单#18）。

        投影读于 planner.decide() 之前、事件却写于 LLM 返回之后，而事件流按
        occurred_at 排序（Projector._fetch）。若取写入时刻，LLM 往返期间到达
        的消息会排到决策事件**之前**——那些消息根本没进本拍 context，却被读
        成"这拍已经看过"（人连发的第二句因此被吞），`<my-thought>` 行也会渲染
        到它们之后。unseen 标签删除后行位置是唯一判据，本条时间戳语义即其
        地基，故设回归护栏。
        """
        captured: list[Any] = []
        loop = AgentLoop(
            scope_key="group:12345",
            planner=_SlowIdlePlanner(),
            session_factory=_factory_for(captured),
        )
        loop.start()
        loop.wake(immediate=True)
        for _ in range(200):
            await asyncio.sleep(0.01)
            if len(captured) >= 4:
                break
        await loop.stop()

        by_type = {
            _values_of(stmt).get("type"): _values_of(stmt) for stmt in captured
        }
        started = by_type["runtime.tick_started"]["occurred_at"]
        decision = by_type["agent.decision_emitted"]["occurred_at"]
        ended = by_type["runtime.tick_ended"]["occurred_at"]

        # tick_started 取默认的写入时刻，写在 now=china_now() 之后；decision
        # 回填 now，因此必然 <= tick_started。旧行为（取写入时刻）会比它晚
        # 整整一个 planner 延迟，这条断言即失败。
        self.assertLessEqual(decision, started)
        # 反证 planner 确实慢过一拍：tick 收尾比决策时间戳晚至少一个 DELAY，
        # 说明上面的 <= 不是"planner 快到看不出差别"蒙对的。
        self.assertGreaterEqual(
            (ended - decision).total_seconds(), _SlowIdlePlanner.DELAY
        )
        # idle_decision 属同拍动作事件，与 decision 同步回填（2026-07-27）。
        idle = by_type["agent.idle_decision"]["occurred_at"]
        self.assertEqual(idle, decision)

    async def test_action_timestamps_are_tick_start_not_write_time(self) -> None:
        """_apply_actions 派生的动作事件（task_created / tool_called / 自动
        推进的 task_state_changed）occurred_at = 本拍投影时刻（2026-07-27，
        补齐待办清单#18 的另一半）。

        #18 只回填了 decision_emitted：<my-thought> 行归位了，但真正携带授权
        内容的 <tool-call> 行仍取写入时刻，LLM 往返期间到达的消息排在它之前
        ——下一拍 Planner 与 Replyer（折入条款以授权行位置为参照）都把没进
        本拍 context 的消息读成"落稿前已看过、有意不接"，连发的后续消息就此
        既不被补授权也不被折入。行位置是"处理过没有"的唯一判据（unseen 已
        删），因此动作事件必须与 decision 同锚。
        """
        captured: list[Any] = []
        loop = AgentLoop(
            scope_key="group:12345",
            planner=_SlowCallToolPlanner(),
            session_factory=_factory_for(captured),
        )
        loop.start()
        loop.wake(immediate=True)
        for _ in range(200):
            await asyncio.sleep(0.01)
            if len(captured) >= 6:
                break
        await loop.stop()

        by_type = {
            _values_of(stmt).get("type"): _values_of(stmt) for stmt in captured
        }
        started = by_type["runtime.tick_started"]["occurred_at"]
        decision = by_type["agent.decision_emitted"]["occurred_at"]
        ended = by_type["runtime.tick_ended"]["occurred_at"]
        for event_type in (
            "agent.task_created",
            "agent.tool_called",
            "agent.task_state_changed",
        ):
            action_at = by_type[event_type]["occurred_at"]
            self.assertEqual(action_at, decision, event_type)
            self.assertLessEqual(action_at, started, event_type)
        # 反证 planner 确实慢过一拍（同 decision 测试的护栏语义）。
        self.assertGreaterEqual(
            (ended - decision).total_seconds(), _SlowCallToolPlanner.DELAY
        )

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
        loop.wake(immediate=True)
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
        loop.wake(immediate=True)
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


class WakeDebounceTests(unittest.IsolatedAsyncioTestCase):
    """唤醒攒批窗口（2026-07-28）。

    存在的理由不是省 tick —— asyncio.Event 早就能把"上一拍还在跑"期间的多次
    唤醒并成一次。堵的是 loop **空闲**时第一条消息立刻开拍这个洞：QQ 上一句话
    拆成三条发是常态，不等一等就会对着半截话表态。

    窗口值用 patch 压到毫秒级跑，避免测试挂在真实的 2s 上。
    """

    @staticmethod
    async def _settle(captured: list[Any], count: int, budget: float) -> None:
        deadline = budget
        while deadline > 0 and len(captured) < count:
            await asyncio.sleep(0.01)
            deadline -= 0.01

    async def _tick_count(self, captured: list[Any]) -> int:
        return sum(
            1
            for stmt in captured
            if _values_of(stmt).get("type") == "runtime.tick_started"
        )

    async def test_plain_wake_waits_for_quiet_window(self) -> None:
        captured: list[Any] = []
        loop = AgentLoop(
            scope_key="group:12345",
            planner=FakeIdlePlanner(),
            session_factory=_factory_for(captured),
        )
        with patch(
            "qqbot.services.agent_loop.loop._WAKE_DEBOUNCE_SECONDS", 0.2
        ):
            loop.start()
            loop.wake()
            # 窗口未到：还不该开拍
            await asyncio.sleep(0.05)
            self.assertEqual(await self._tick_count(captured), 0)
            # 安静下来之后开拍
            await self._settle(captured, 4, budget=1.0)
            await loop.stop()
        self.assertEqual(await self._tick_count(captured), 1)

    async def test_burst_of_wakes_collapses_into_one_tick(self) -> None:
        """一句话拆成三条发 → 只开一拍，且这拍看得到全部三条。"""
        captured: list[Any] = []
        loop = AgentLoop(
            scope_key="group:12345",
            planner=FakeIdlePlanner(),
            session_factory=_factory_for(captured),
        )
        with patch(
            "qqbot.services.agent_loop.loop._WAKE_DEBOUNCE_SECONDS", 0.2
        ):
            loop.start()
            for _ in range(3):
                loop.wake()
                await asyncio.sleep(0.05)  # 窗口内陆续到达 → 顺延 deadline
            self.assertEqual(await self._tick_count(captured), 0)
            await self._settle(captured, 4, budget=1.0)
            await loop.stop()
        self.assertEqual(await self._tick_count(captured), 1)

    async def test_immediate_wake_bypasses_window(self) -> None:
        """工具批次收口这类唤醒直接开拍：结果已经落库，没有可攒的东西。"""
        captured: list[Any] = []
        loop = AgentLoop(
            scope_key="group:12345",
            planner=FakeIdlePlanner(),
            session_factory=_factory_for(captured),
        )
        with patch(
            "qqbot.services.agent_loop.loop._WAKE_DEBOUNCE_SECONDS", 5.0
        ):
            loop.start()
            loop.wake(immediate=True)
            await self._settle(captured, 4, budget=1.0)
            await loop.stop()
        self.assertEqual(await self._tick_count(captured), 1)

    async def test_continuous_wakes_capped_by_max_delay(self) -> None:
        """持续刷屏不能把 tick 饿死：deadline 顺延有硬上限，到点必开拍。"""
        captured: list[Any] = []
        loop = AgentLoop(
            scope_key="group:12345",
            planner=FakeIdlePlanner(),
            session_factory=_factory_for(captured),
        )
        with (
            patch(
                "qqbot.services.agent_loop.loop._WAKE_DEBOUNCE_SECONDS", 0.2
            ),
            patch(
                "qqbot.services.agent_loop.loop._WAKE_MAX_DELAY_SECONDS", 0.3
            ),
        ):
            loop.start()
            # 每 50ms 一次唤醒，始终不让窗口安静下来，持续 0.6s（> max delay）
            for _ in range(12):
                loop.wake()
                await asyncio.sleep(0.05)
            await self._settle(captured, 4, budget=1.0)
            await loop.stop()
        self.assertGreaterEqual(await self._tick_count(captured), 1)


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
        await sup.wake("group:12345", immediate=True)
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


class MemoryCompactorWiringTests(unittest.IsolatedAsyncioTestCase):
    """记忆压缩器接线（记忆系统契约 §4.1/§4.2）：开关默认关 = 不构造；
    打开 = start 挂起等待触顶 + 投影装探针 + stop 收掉；未启用时 notify
    安全 no-op。"""

    async def test_disabled_by_default_no_compactor(self) -> None:
        captured: list[Any] = []
        sup = LoopSupervisor(
            planner=FakeIdlePlanner(),
            session_factory=_factory_for(captured),
        )
        await sup.start()
        self.assertIsNone(sup._memory_compactor)
        sup.notify_compaction("group:1", 250)  # 未启用：安全 no-op
        await sup.stop()

    async def test_enabled_env_wires_compactor_and_probe(self) -> None:
        import os

        from qqbot.services.agent_loop.projection import Projector

        old = os.environ.get("MEMORY_COMPACTION_ENABLED")
        os.environ["MEMORY_COMPACTION_ENABLED"] = "true"
        try:
            captured: list[Any] = []
            projector = Projector(_factory_for(captured))
            sup = LoopSupervisor(
                planner=FakeIdlePlanner(),
                session_factory=_factory_for(captured),
                projector=projector,
            )
            await sup.start()
            self.assertIsNotNone(sup._memory_compactor)
            self.assertIsNotNone(projector._uncovered_notifier)
            await sup.stop()
            self.assertIsNone(sup._memory_compactor)
        finally:
            if old is None:
                os.environ.pop("MEMORY_COMPACTION_ENABLED", None)
            else:
                os.environ["MEMORY_COMPACTION_ENABLED"] = old


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

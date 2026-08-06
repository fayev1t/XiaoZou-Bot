"""Contract for the v2 AgentLoop skeleton (LoopSupervisor + AgentLoop + planner).

Pure unit-level; DB is faked by a recording session, no nonebot needed.

Verifies the skeleton produces the expected sequence of internal events on
one empty-program tick:
  runtime.tick_started → agent.decision_emitted → agent.program_completed
  → runtime.tick_ended
all sharing the same correlation_id.

Also verifies:
- LoopSupervisor lazy-instantiates GroupAgentLoop on wake.
- LoopSupervisor silently drops private:* wakes.
- LoopSupervisor.start() spawns the system loop up front.
- EventIngest publishes only newly committed SystemEvent values; duplicate
  inserts publish nothing, and plugin wiring owns scope-to-wake translation.
- scope_key parser handles all three AgentLoop scopes.
- 唤醒攒批窗口（2026-07-28 引入，2026-08-01 改固定窗口）：默认 wake() 由第一次
  唤醒开窗、到点才开拍，窗口内的唤醒并入同一拍且不顺延，immediate=True 绕过
  窗口，持续唤醒下每个窗口到点照常开拍（不会被顺延饿死）。
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from qqbot.services.agent_loop import (
    AgentLoop,
    DecisionOutput,
    LoopSupervisor,
)
from qqbot.services.agent_loop.event_writer import WakeMode, parse_scope_key
from qqbot.services.agent_loop.tool_registry import (
    BaseTool,
    ToolOutcome,
    ToolRegistry,
)


class _FakeIdlePlanner:
    """空程序 planner：只验证循环接线（events → tick → events），不碰 LLM。

    原为生产包里的 qqbot/services/agent_loop/planner.py::FakeIdlePlanner，
    2026-07-31 迁入测试——它从来没有生产消费者，LoopSupervisor 装的是
    LLMPlanner。按本目录惯例内联在用到它的测试文件里，不建共享 fixture 模块。
    """

    async def decide(self, context: Any) -> DecisionOutput:
        _ = context
        return DecisionOutput(program="# bootstrap skeleton: intentionally idle")


class _EmptyResult:
    """Empty result compatible with the recovery/backfill SELECT consumers."""

    def mappings(self) -> "_EmptyResult":
        return self

    def scalars(self) -> "_EmptyResult":
        return self

    def all(self) -> list:
        return []

    def first(self) -> None:
        return None


class _RecordingSession:
    """async session double that captures every executed insert statement.

    Reads used by ReplyExecutor, task backfill, and program crash recovery are
    ignored and return an empty result. Only mutating statements are appended
    to ``store``.
    """

    def __init__(self, store: list[Any]) -> None:
        self._store = store

    async def execute(self, stmt: Any, params: dict | None = None) -> Any:
        from sqlalchemy.sql.elements import TextClause

        _ = params
        if isinstance(stmt, TextClause) or bool(getattr(stmt, "is_select", False)):
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


class _SlowIdlePlanner:
    """模拟 LLM 往返：decide() 里睡一段可观测的时间再返回空程序。

    用来把"投影时刻"和"决策写入时刻"拉开到断言可分辨的距离。
    """

    DELAY = 0.15

    async def decide(self, context: Any) -> Any:
        _ = context
        await asyncio.sleep(self.DELAY)
        return DecisionOutput(program="# slow idle")


class _TimestampEffect(BaseTool):
    name = "timestamp_effect"
    program_kind = "effect"
    arguments_schema = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    result_schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "additionalProperties": False,
    }

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        return ToolOutcome.success({"ok": True})


class _SlowCallToolPlanner:
    """模拟 LLM 往返后在本拍内执行一个 effect。"""

    DELAY = 0.15

    async def decide(self, context: Any) -> Any:
        _ = context
        await asyncio.sleep(self.DELAY)
        return DecisionOutput(program="timestamp_effect()")


class AgentLoopSkeletonTickTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_wake_produces_idle_tick_event_chain(self) -> None:
        captured: list[Any] = []
        loop = AgentLoop(
            scope_key="group:12345",
            planner=_FakeIdlePlanner(),
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

        # 空程序仍有独立 terminal；不再写 idle_decision。
        types = [_values_of(stmt).get("type") for stmt in captured]
        self.assertEqual(
            types,
            [
                "runtime.tick_started",
                "agent.decision_emitted",
                "agent.program_completed",
                "runtime.tick_ended",
            ],
        )
        decision_payload = _values_of(captured[1]).get("payload")
        self.assertEqual(
            decision_payload["program"],
            "# bootstrap skeleton: intentionally idle",
        )
        self.assertIn("program_sha256", decision_payload)

        # 同一 tick 内 correlation_id 一致
        corrs = {_values_of(stmt).get("correlation_id") for stmt in captured}
        self.assertEqual(len(corrs), 1)

        # decision_emitted → program_completed 因果链
        decision_id = _values_of(captured[1]).get("event_id")
        terminal_caus = _values_of(captured[2]).get("causation_id")
        self.assertEqual(terminal_caus, decision_id)

        # tick_started → tick_ended 因果链
        tick_started_id = _values_of(captured[0]).get("event_id")
        tick_ended_caus = _values_of(captured[3]).get("causation_id")
        self.assertEqual(tick_ended_caus, tick_started_id)

    async def test_decision_timestamp_is_tick_start_not_write_time(self) -> None:
        """agent.decision_emitted.occurred_at = 本拍**投影时刻**，不是写入时刻
        （2026-07-24，待办清单#18）。

        投影读于 planner.decide() 之前、事件却写于 LLM 返回之后，而事件流按
        occurred_at 排序（Projector._fetch）。若取写入时刻，LLM 往返期间到达
        的消息会排到决策事件**之前**，事件流的因果顺序即与本拍真实看到的内容
        不符。

        2026-08-02 删除 `<message unseen="true">` 后本条护栏**不随之取消**：
        decision_emitted 虽不再投影、也不再充当水位线，但它与同拍
        ``tool_called`` 意图必须同刻，后者要进入时间线（事件系统设计.md
        §时间戳约束）。
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
        # program terminal 陈述执行完成，取实际完成时刻而非投影锚点。
        terminal = by_type["agent.program_completed"]["occurred_at"]
        self.assertGreaterEqual(terminal, decision)

    async def test_effect_intent_timestamp_is_tick_start_not_write_time(self) -> None:
        """程序 effect 的意图事件仍锚定本拍投影时刻。

        ``tool_called`` 是模型拍板的 effect 调用行，必须和 decision 同锚；
        terminal 与 program terminal 则记录真实完成时刻。
        """
        captured: list[Any] = []
        registry = ToolRegistry()
        registry.register(_TimestampEffect)
        loop = AgentLoop(
            scope_key="group:12345",
            planner=_SlowCallToolPlanner(),
            session_factory=_factory_for(captured),
            tool_registry=registry,
        )
        loop.start()
        loop.wake(immediate=True)
        for _ in range(200):
            await asyncio.sleep(0.01)
            if any(
                _values_of(stmt).get("type") == "runtime.tick_ended"
                for stmt in captured
                if getattr(stmt, "table", None) is not None
            ):
                break
        await loop.stop()

        by_type = {
            _values_of(stmt).get("type"): _values_of(stmt) for stmt in captured
        }
        started = by_type["runtime.tick_started"]["occurred_at"]
        decision = by_type["agent.decision_emitted"]["occurred_at"]
        ended = by_type["runtime.tick_ended"]["occurred_at"]
        called_at = by_type["agent.tool_called"]["occurred_at"]
        self.assertEqual(called_at, decision)
        self.assertLessEqual(called_at, started)
        self.assertGreaterEqual(
            by_type["agent.tool_result"]["occurred_at"], called_at
        )
        # 反证 planner 确实慢过一拍（同 decision 测试的护栏语义）。
        self.assertGreaterEqual(
            (ended - decision).total_seconds(), _SlowCallToolPlanner.DELAY
        )

    async def test_loop_idle_when_not_waked(self) -> None:
        captured: list[Any] = []
        loop = AgentLoop(
            scope_key="group:1",
            planner=_FakeIdlePlanner(),
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
            planner=_FakeIdlePlanner(),
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
            planner=_FakeIdlePlanner(),
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
        # 正常空程序事件链应当落地，不被 resolver 异常掐断。
        types = [_values_of(stmt).get("type") for stmt in captured]
        self.assertIn("runtime.tick_started", types)
        self.assertIn("runtime.tick_ended", types)


class WakeBatchWindowTests(unittest.IsolatedAsyncioTestCase):
    """唤醒攒批窗口（2026-07-28 引入，2026-08-01 由滑动改固定）。

    存在的理由不是省 tick —— asyncio.Event 早就能把"上一拍还在跑"期间的多次
    唤醒并成一次。堵的是 loop **空闲**时第一条消息立刻开拍这个洞：QQ 上一句话
    拆成三条发是常态，不等一等就会对着半截话表态。

    固定窗口：第一次唤醒开窗，窗口内的唤醒并入本窗、不顺延 deadline。开拍延迟
    因此有界，不再需要（也不再有）防饿死的封顶常量。

    窗口值用 patch 压到毫秒级跑，避免测试挂在真实的 3s 上。
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

    async def test_plain_wake_waits_for_batch_window(self) -> None:
        captured: list[Any] = []
        loop = AgentLoop(
            scope_key="group:12345",
            planner=_FakeIdlePlanner(),
            session_factory=_factory_for(captured),
        )
        with patch(
            "qqbot.services.agent_loop.loop._WAKE_BATCH_WINDOW_SECONDS", 0.2
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
            planner=_FakeIdlePlanner(),
            session_factory=_factory_for(captured),
        )
        with patch(
            "qqbot.services.agent_loop.loop._WAKE_BATCH_WINDOW_SECONDS", 0.2
        ):
            loop.start()
            for _ in range(3):
                loop.wake()
                await asyncio.sleep(0.05)  # 窗口内陆续到达 → 并入本窗，不顺延
            self.assertEqual(await self._tick_count(captured), 0)
            await self._settle(captured, 4, budget=1.0)
            await loop.stop()
        self.assertEqual(await self._tick_count(captured), 1)

    async def test_immediate_wake_bypasses_window(self) -> None:
        """reply 到点等完成事实直接开拍：结果已落库，没有可攒的东西。"""
        captured: list[Any] = []
        loop = AgentLoop(
            scope_key="group:12345",
            planner=_FakeIdlePlanner(),
            session_factory=_factory_for(captured),
        )
        with patch(
            "qqbot.services.agent_loop.loop._WAKE_BATCH_WINDOW_SECONDS", 5.0
        ):
            loop.start()
            loop.wake(immediate=True)
            await self._settle(captured, 4, budget=1.0)
            await loop.stop()
        self.assertEqual(await self._tick_count(captured), 1)

    async def test_continuous_wakes_tick_once_per_window(self) -> None:
        """持续刷屏不能把 tick 饿死：窗口不被顺延，到点就开拍，之后的唤醒开一
        个新窗口。这也是固定窗口不再需要封顶常量的原因。"""
        captured: list[Any] = []
        loop = AgentLoop(
            scope_key="group:12345",
            planner=_FakeIdlePlanner(),
            session_factory=_factory_for(captured),
        )
        with patch(
            "qqbot.services.agent_loop.loop._WAKE_BATCH_WINDOW_SECONDS", 0.2
        ):
            loop.start()
            # 每 50ms 一次唤醒持续 0.6s：旧的滑动实现会一路顺延到封顶才开一拍，
            # 固定窗口下 0.2s 的窗口在这段时间里至少轮到两次。
            for _ in range(12):
                loop.wake()
                await asyncio.sleep(0.05)
            await self._settle(captured, 4, budget=1.0)
            await loop.stop()
        self.assertGreaterEqual(await self._tick_count(captured), 2)


class LoopSupervisorContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_spawns_system_loop(self) -> None:
        captured: list[Any] = []
        sup = LoopSupervisor(
            planner=_FakeIdlePlanner(),
            session_factory=_factory_for(captured),
        )
        await sup.start()
        self.assertTrue(sup.started)
        self.assertEqual(sup.loop_count, 1)
        await sup.stop()

    async def test_wake_lazy_creates_group_loop(self) -> None:
        captured: list[Any] = []
        sup = LoopSupervisor(
            planner=_FakeIdlePlanner(),
            session_factory=_factory_for(captured),
        )
        await sup.start()
        await sup.wake("group:12345", mode=WakeMode.IMMEDIATE)
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
            planner=_FakeIdlePlanner(),
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
            planner=_FakeIdlePlanner(),
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


class SupervisorWakeModeTests(unittest.IsolatedAsyncioTestCase):
    """唤醒入口统一成 wake(mode=...)（2026-08-04）。

    此前是 wake / _wake_immediate / _wake_no_arm 三个方法，后两个明明是私有
    却被当回调注入给 ReplyExecutor 与 SilenceWatcher。现在只剩一个入口，
    模式由 waker(mode) 在装配时 partial 绑定，注入出去的回调统一是
    (scope_key) -> Awaitable[None]。
    """

    class _SpyWatcher:
        """只记录活动通知的静默计时器替身。"""

        def __init__(self) -> None:
            self.armed: list[str] = []
            self.enabled = True

        def notify_activity(self, scope_key: str) -> None:
            self.armed.append(scope_key)

        async def stop(self) -> None:
            return None

    async def _supervisor(self) -> tuple[Any, "_SpyWatcher"]:
        sup = LoopSupervisor(
            planner=_FakeIdlePlanner(),
            session_factory=_factory_for([]),
        )
        await sup.start()
        watcher = self._SpyWatcher()
        sup._silence_watcher = watcher
        return sup, watcher

    async def test_batched_and_immediate_rearm_the_silence_timer(self) -> None:
        """普通唤醒都算"有动静"，静默计时器必须重排。"""
        sup, watcher = await self._supervisor()
        await sup.wake("group:1")
        await sup.wake("group:2", mode=WakeMode.IMMEDIATE)
        await sup.stop()
        self.assertEqual(watcher.armed, ["group:1", "group:2"])

    async def test_no_arm_mode_does_not_rearm_the_silence_timer(self) -> None:
        """静默叫醒自己不能重置自己的计时器。

        走普通路径会把这次叫醒当成"有动静"重新武装，于是一段静默里每隔一个
        阈值就响一次；"一段静默只响一次"正是靠这条旁路成立的。
        """
        sup, watcher = await self._supervisor()
        await sup.wake("group:1", mode=WakeMode.IMMEDIATE_NO_ARM)
        await sup.stop()
        self.assertEqual(watcher.armed, [])

    async def test_waker_binds_mode_and_hides_it_from_producers(self) -> None:
        """注入给生产者的回调只接受 scope_key —— 模式是装配决定，不是调用参数。"""
        sup, watcher = await self._supervisor()
        wake = sup.waker(WakeMode.IMMEDIATE_NO_ARM)
        await wake("group:1")  # 单参数调用，生产者不认识 WakeMode
        await sup.stop()
        self.assertEqual(watcher.armed, [])


class MemoryCompactorWiringTests(unittest.IsolatedAsyncioTestCase):
    """记忆压缩器接线（记忆系统契约 §4.1/§4.2）：开关默认关 = 不构造；
    打开 = start 挂起等待触顶 + 投影装探针 + stop 收掉；未启用时 notify
    安全 no-op。"""

    async def test_disabled_by_default_no_compactor(self) -> None:
        import os

        # 显式关掉开关再断言（与下面 enabled 用例对称）。原先靠"环境里没配"
        # 隐式成立，而部署机的 .env 里 MEMORY_COMPACTION_ENABLED=true，
        # memory_compaction_enabled() 读的就是真实 env —— 这条在开了压缩的
        # 机器上必然失败。测试必须自己控制前置条件，不能继承部署配置。
        old = os.environ.get("MEMORY_COMPACTION_ENABLED")
        os.environ["MEMORY_COMPACTION_ENABLED"] = "false"
        try:
            captured: list[Any] = []
            sup = LoopSupervisor(
                planner=_FakeIdlePlanner(),
                session_factory=_factory_for(captured),
            )
            await sup.start()
            self.assertIsNone(sup._memory_compactor)
            sup.notify_compaction("group:1", 250)  # 未启用：安全 no-op
            await sup.stop()
        finally:
            if old is None:
                os.environ.pop("MEMORY_COMPACTION_ENABLED", None)
            else:
                os.environ["MEMORY_COMPACTION_ENABLED"] = old

    async def test_enabled_env_wires_compactor_and_probe(self) -> None:
        import os

        from qqbot.services.agent_loop.projection import Projector

        old = os.environ.get("MEMORY_COMPACTION_ENABLED")
        os.environ["MEMORY_COMPACTION_ENABLED"] = "true"
        try:
            captured: list[Any] = []
            projector = Projector(_factory_for(captured))
            sup = LoopSupervisor(
                planner=_FakeIdlePlanner(),
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


class IngestSupervisorIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_ingest_notifies_only_with_committed_internal_event(self) -> None:
        from qqbot.services.event_ingest import EventIngest
        from qqbot.services.event_ingest.mappers import build_default_registry

        committed: list[Any] = []

        async def notify(event: Any) -> None:
            committed.append(event)

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
            committed_notifier=notify,
        )
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
        self.assertEqual(committed, [result.event])
        self.assertEqual(committed[0].scope, "group")
        self.assertEqual(committed[0].group_id, 999)

    async def test_private_event_is_still_published_as_committed(self) -> None:
        from qqbot.services.event_ingest import EventIngest
        from qqbot.services.event_ingest.mappers import build_default_registry

        committed: list[Any] = []

        async def notify(event: Any) -> None:
            committed.append(event)

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
            committed_notifier=notify,
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
        self.assertEqual(committed, [result.event])
        self.assertEqual(committed[0].scope, "private")

    async def test_ingest_does_not_notify_for_duplicate(self) -> None:
        from qqbot.services.event_ingest import EventIngest
        from qqbot.services.event_ingest.mappers import build_default_registry

        committed: list[Any] = []

        async def notify(event: Any) -> None:
            committed.append(event)

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
            committed_notifier=notify,
        )
        event = SimpleNamespace(
            post_type="message", message_type="group", sub_type="normal",
            time=1716700000, self_id=10000, message_id=12345,
            group_id=999, user_id=222, raw_message="", message=[], sender=None,
        )
        result = await ingest.ingest(event)
        self.assertEqual(result.status, "duplicate")
        self.assertEqual(committed, [])


class _ScriptedPlanner:
    """按脚本逐拍返回程序；脚本用尽后一律返回空程序。

    空程序收尾是自续拍的不动点，因此即使被测代码有 bug 也不会把测试跑成死循环。
    """

    def __init__(self, programs: list[str]) -> None:
        self._programs = list(programs)

    async def decide(self, context: Any) -> DecisionOutput:
        _ = context
        if self._programs:
            return DecisionOutput(program=self._programs.pop(0))
        return DecisionOutput(program="# nothing left to do")


class _AlwaysCallingPlanner:
    """每拍都调用一次 effect —— 只有上界才能让它停下来。"""

    async def decide(self, context: Any) -> DecisionOutput:
        _ = context
        return DecisionOutput(program="timestamp_effect()")


class _TimestampQuery(BaseTool):
    name = "timestamp_query"
    program_kind = "query"
    arguments_schema = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    result_schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "additionalProperties": False,
    }

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        return ToolOutcome.success({"ok": True})


class _FailingEffect(BaseTool):
    name = "failing_effect"
    program_kind = "effect"
    arguments_schema = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    result_schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "additionalProperties": False,
    }

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        return ToolOutcome.failure("internal_tool_error", "boom")


class ContinuationMaxTicksResolverTests(unittest.TestCase):
    """``AGENT_CONTINUATION_MAX_TICKS`` 解析（任务与决策契约 §1.2）。"""

    def _resolve(self, raw: str | None) -> int | None:
        with patch(
            "qqbot.services.agent_loop.loop.get_env_value", return_value=raw
        ):
            from qqbot.services.agent_loop.loop import continuation_max_ticks

            return continuation_max_ticks()

    def test_unset_is_unlimited(self) -> None:
        self.assertIsNone(self._resolve(None))

    def test_blank_is_unlimited(self) -> None:
        self.assertIsNone(self._resolve("   "))

    def test_zero_disables(self) -> None:
        self.assertEqual(self._resolve("0"), 0)

    def test_positive_is_the_cap(self) -> None:
        self.assertEqual(self._resolve("5"), 5)

    def test_negative_clamps_to_disabled(self) -> None:
        self.assertEqual(self._resolve("-3"), 0)

    def test_garbage_falls_back_to_unlimited(self) -> None:
        self.assertIsNone(self._resolve("many"))


class ContinuationTickTests(unittest.IsolatedAsyncioTestCase):
    """自续拍（2026-08-04，任务与决策契约 §1.2）。

    程序调用过函数 → 本拍收尾后自行再开一拍；某一拍一个函数都不调用 → 链条结束。
    判据是 ``ProgramTrace.calls``：Query 与 Effect 同等、成功与失败同等。
    """

    @staticmethod
    def _tick_count(captured: list[Any]) -> int:
        return sum(
            1
            for stmt in captured
            if _values_of(stmt).get("type") == "runtime.tick_started"
        )

    async def _run(
        self,
        planner: Any,
        *,
        registry: ToolRegistry | None = None,
        expect_ticks: int,
        max_ticks: int | None = None,
    ) -> list[Any]:
        """开一次外部 wake，跑到链条停稳，返回捕获到的语句。

        settle 预算给到期望拍数之后仍多等一截，好让"多续了一拍"这类回归表现为
        断言失败而不是恰好没观测到。
        """
        captured: list[Any] = []
        with patch(
            "qqbot.services.agent_loop.loop.continuation_max_ticks",
            return_value=max_ticks,
        ):
            loop = AgentLoop(
                scope_key="group:12345",
                planner=planner,
                session_factory=_factory_for(captured),
                tool_registry=registry,
            )
        loop.start()
        loop.wake(immediate=True)
        for _ in range(120):
            await asyncio.sleep(0.01)
            if self._tick_count(captured) > expect_ticks:
                break
        await asyncio.sleep(0.05)
        await loop.stop()
        return captured

    async def test_empty_program_does_not_continue(self) -> None:
        """空程序是不动点：一次外部唤醒只换来一拍。"""
        captured = await self._run(_FakeIdlePlanner(), expect_ticks=1)
        self.assertEqual(self._tick_count(captured), 1)

    async def test_effect_call_continues_until_empty_program(self) -> None:
        registry = ToolRegistry()
        registry.register(_TimestampEffect)
        captured = await self._run(
            _ScriptedPlanner(["timestamp_effect()"]),
            registry=registry,
            expect_ticks=2,
        )
        # 第一拍调用 → 自续第二拍 → 第二拍空程序 → 停。
        self.assertEqual(self._tick_count(captured), 2)

    async def test_query_only_program_continues(self) -> None:
        """Query 同样续拍 —— 「查完接着办」正是这条机制存在的理由。"""
        registry = ToolRegistry()
        registry.register(_TimestampQuery)
        captured = await self._run(
            _ScriptedPlanner(["timestamp_query()"]),
            registry=registry,
            expect_ticks=2,
        )
        self.assertEqual(self._tick_count(captured), 2)

    async def test_failed_call_continues(self) -> None:
        """失败调用照样续拍：中止余下程序意味着她当拍接不住，得换一拍再判断。"""
        registry = ToolRegistry()
        registry.register(_FailingEffect)
        captured = await self._run(
            _ScriptedPlanner(["failing_effect()"]),
            registry=registry,
            expect_ticks=2,
        )
        self.assertEqual(self._tick_count(captured), 2)
        types = [_values_of(stmt).get("type") for stmt in captured]
        self.assertIn("agent.program_failed", types)

    async def test_max_ticks_caps_the_chain(self) -> None:
        """上界只约束一段自转的长度：1 → 外部一拍 + 自续一拍后必须停。"""
        registry = ToolRegistry()
        registry.register(_TimestampEffect)
        captured = await self._run(
            _AlwaysCallingPlanner(),
            registry=registry,
            expect_ticks=2,
            max_ticks=1,
        )
        self.assertEqual(self._tick_count(captured), 2)

    async def test_zero_max_disables_continuation(self) -> None:
        """0 = 关闭自续拍，退回纯事件驱动。"""
        registry = ToolRegistry()
        registry.register(_TimestampEffect)
        captured = await self._run(
            _AlwaysCallingPlanner(),
            registry=registry,
            expect_ticks=1,
            max_ticks=0,
        )
        self.assertEqual(self._tick_count(captured), 1)

    async def test_external_wake_resets_continuation_depth(self) -> None:
        """外部唤醒 = 新一段活动，自转计数归零，上界重新起算。"""
        with patch(
            "qqbot.services.agent_loop.loop.continuation_max_ticks",
            return_value=2,
        ):
            loop = AgentLoop(
                scope_key="group:12345",
                planner=_FakeIdlePlanner(),
                session_factory=_factory_for([]),
            )
        loop._continuation_depth = 2
        self.assertFalse(loop._wake_continuation())
        loop.wake(immediate=True)
        self.assertEqual(loop._continuation_depth, 0)
        self.assertTrue(loop._wake_continuation())

    async def test_stopped_loop_does_not_continue(self) -> None:
        with patch(
            "qqbot.services.agent_loop.loop.continuation_max_ticks",
            return_value=None,
        ):
            loop = AgentLoop(
                scope_key="group:12345",
                planner=_FakeIdlePlanner(),
                session_factory=_factory_for([]),
            )
        loop._stopped = True
        self.assertFalse(loop._wake_continuation())


if __name__ == "__main__":
    unittest.main()

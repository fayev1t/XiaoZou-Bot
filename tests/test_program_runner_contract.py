"""拍间并行 / 拍内串行的派发契约（2026-08-14）。

- 不同决策拍产出的程序**可以同时在跑**（本文件）；
- 单段程序内部仍按源码顺序逐个调用（ProgramExecutor 的既有契约，见
  ``test_program_runtime_contract``，此处不重复）；
- 同 scope 的出站发送互斥，气泡不交错（``SendSerializationTests``）。
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime
from typing import Any
from unittest.mock import patch
from zoneinfo import ZoneInfo

from qqbot.services.agent_loop.decision import DecisionContext, DecisionOutput
from qqbot.services.agent_loop.loop import AgentLoop
from qqbot.services.agent_loop.outbound_messages import send_all
from qqbot.services.agent_loop.program_ast import preflight
from qqbot.services.agent_loop.program_runner import ProgramRunner, QueuedProgram
from qqbot.services.agent_loop.tool_registry import (
    BaseTool,
    ToolOutcome,
    ToolRegistry,
)

NOW = datetime(2026, 8, 14, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def _item(decision_id: str) -> QueuedProgram:
    prepared = preflight("# idle", ToolRegistry(), "group")
    return QueuedProgram(
        decision_id=decision_id,
        scope_key="group:1",
        correlation_id="CORR",
        prepared=prepared,
        context=DecisionContext(
            scope_key="group:1",
            correlation_id="CORR",
            tick_seq=1,
            now=NOW,
        ),
        enqueued_at=NOW,
    )


class ProgramRunnerConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_programs_from_different_ticks_run_concurrently(self) -> None:
        """B 必须能在 A 仍在飞时跑完 —— 这是拍间并行的判据。

        2026-08-14 前这里断言的是严格 FIFO 串行。改并行的理由：慢调用全是
        只读的（websearch / webfetch / look_at_image），副作用调用全是亚秒级
        的；串行会让她想说的那句话排在一次网页检索后面。
        """
        order: list[str] = []
        a_started = asyncio.Event()
        a_release = asyncio.Event()
        b_done = asyncio.Event()

        async def execute(item: QueuedProgram) -> None:
            order.append(f"start:{item.decision_id}")
            if item.decision_id == "A":
                a_started.set()
                await a_release.wait()
            order.append(f"end:{item.decision_id}")
            if item.decision_id == "B":
                b_done.set()

        wakes = 0

        def on_finished() -> bool:
            nonlocal wakes
            wakes += 1
            return True

        runner = ProgramRunner(
            scope_key="group:1",
            execute=execute,
            on_finished=on_finished,
            max_concurrency=4,
        )
        runner.start()
        runner.enqueue(_item("A"))
        await asyncio.wait_for(a_started.wait(), timeout=1.0)
        runner.enqueue(_item("B"))
        await asyncio.wait_for(b_done.wait(), timeout=1.0)
        await asyncio.sleep(0.02)  # 让 B 的 task 收尾、done_callback 落地

        self.assertEqual(order, ["start:A", "start:B", "end:B"])
        self.assertEqual(runner.queue_depth, 1)  # A 仍在飞
        self.assertEqual(wakes, 1)  # 只有 B 叫醒过 scope

        a_release.set()
        for _ in range(50):
            await asyncio.sleep(0.01)
            if wakes >= 2:
                break
        await runner.stop()
        self.assertEqual(order, ["start:A", "start:B", "end:B", "end:A"])
        self.assertEqual(wakes, 2)

    async def test_max_concurrency_caps_parallel_programs(self) -> None:
        """并发上限防的是 fan-out（每段程序都可能挂着 HTTP + LLM 调用）。"""
        running = 0
        peak = 0
        release = asyncio.Event()

        async def execute(item: QueuedProgram) -> None:
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await release.wait()
            running -= 1

        runner = ProgramRunner(
            scope_key="group:1",
            execute=execute,
            on_finished=lambda: True,
            max_concurrency=2,
        )
        runner.start()
        for decision_id in ("A", "B", "C"):
            runner.enqueue(_item(decision_id))
        for _ in range(50):
            await asyncio.sleep(0.01)
            if running >= 2:
                break
        await asyncio.sleep(0.05)  # 给 C 抢跑的机会

        self.assertEqual(runner.queue_depth, 3)  # 三段都已派发
        self.assertEqual(peak, 2)  # 但同时在跑的只有两段
        release.set()
        await runner.stop()

    async def test_serial_mode_is_still_reachable(self) -> None:
        """``AGENT_PROGRAM_MAX_CONCURRENCY=1`` 退回串行，作为线上急停开关。"""
        order: list[str] = []
        release = asyncio.Event()

        async def execute(item: QueuedProgram) -> None:
            order.append(f"start:{item.decision_id}")
            if item.decision_id == "A":
                await release.wait()
            order.append(f"end:{item.decision_id}")

        runner = ProgramRunner(
            scope_key="group:1",
            execute=execute,
            on_finished=lambda: True,
            max_concurrency=1,
        )
        runner.start()
        runner.enqueue(_item("A"))
        runner.enqueue(_item("B"))
        await asyncio.sleep(0.05)
        self.assertEqual(order, ["start:A"])  # B 被信号量挡在门外
        release.set()
        for _ in range(50):
            await asyncio.sleep(0.01)
            if len(order) == 4:
                break
        await runner.stop()
        self.assertEqual(order, ["start:A", "end:A", "start:B", "end:B"])

    async def test_stop_drops_programs_that_never_started(self) -> None:
        """关停时还卡在信号量上的程序直接丢弃，不补跑。

        它的 ``decision_emitted`` 没有 program terminal，由下次启动的收口器
        写成 ``interrupted/uncertain``——与「永不重放」一致（§5.3）。
        """
        order: list[str] = []
        started = asyncio.Event()
        release = asyncio.Event()

        async def execute(item: QueuedProgram) -> None:
            order.append(f"start:{item.decision_id}")
            if item.decision_id == "A":
                started.set()
                await release.wait()
            order.append(f"end:{item.decision_id}")

        runner = ProgramRunner(
            scope_key="group:1",
            execute=execute,
            on_finished=lambda: True,
            max_concurrency=1,
        )
        runner.start()
        runner.enqueue(_item("A"))
        await asyncio.wait_for(started.wait(), timeout=1.0)
        runner.enqueue(_item("B"))
        release.set()
        await runner.stop()
        self.assertEqual(order, ["start:A", "end:A"])

    async def test_stop_cancels_inflight_after_grace(self) -> None:
        """关停只等一段余量；未收尾的靠启动收口写 interrupted/uncertain。"""
        started = asyncio.Event()

        async def execute(item: QueuedProgram) -> None:
            started.set()
            await asyncio.Event().wait()  # 永不返回

        wakes = 0

        def on_finished() -> bool:
            nonlocal wakes
            wakes += 1
            return True

        runner = ProgramRunner(
            scope_key="group:1",
            execute=execute,
            on_finished=on_finished,
            max_concurrency=2,
        )
        runner.start()
        runner.enqueue(_item("A"))
        await asyncio.wait_for(started.wait(), timeout=1.0)
        with patch(
            "qqbot.services.agent_loop.program_runner._STOP_GRACE_SECONDS", 0.05
        ):
            await runner.stop()
        self.assertEqual(runner.queue_depth, 0)
        self.assertEqual(wakes, 0)  # 被取消的程序不叫醒 scope

    async def test_host_failure_still_wakes_the_scope(self) -> None:
        """``_execute`` 连兜底写 terminal 都炸了，也必须叫醒 scope。

        否则那一拍既没有 program terminal、也没有下一拍，同进程内无人补写
        （收口器只在进程首拍跑一次）。
        """
        wakes = 0

        def on_finished() -> bool:
            nonlocal wakes
            wakes += 1
            return True

        async def execute(item: QueuedProgram) -> None:
            raise RuntimeError("boom")

        runner = ProgramRunner(
            scope_key="group:1",
            execute=execute,
            on_finished=on_finished,
            max_concurrency=2,
        )
        runner.start()
        runner.enqueue(_item("A"))
        await asyncio.sleep(0.05)
        self.assertEqual(wakes, 1)
        await runner.stop()

    async def test_enqueue_after_stop_is_rejected(self) -> None:
        runner = ProgramRunner(
            scope_key="group:1",
            execute=lambda item: asyncio.sleep(0),
            on_finished=lambda: True,
        )
        runner.start()
        await runner.stop()
        with self.assertRaises(RuntimeError):
            runner.enqueue(_item("A"))


class _GateEffect(BaseTool):
    name = "gate"
    arguments_schema = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    # 字段不能叫 ok：2026-08-15 起 ok/error 由结果信封统一注入，工具自己声明
    # 会被 registry 拒绝（见 test_tool_registry_contract）。
    result_schema = {
        "type": "object",
        "properties": {"passed": {"type": "boolean"}},
        "additionalProperties": False,
    }
    started = asyncio.Event()
    release = asyncio.Event()

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        type(self).started.set()
        await type(self).release.wait()
        return ToolOutcome.success({"passed": True})


class _GatePlanner:
    async def decide(self, context: Any) -> DecisionOutput:
        _ = context
        return DecisionOutput(program="gate()")


class DispatchOverlapTests(unittest.IsolatedAsyncioTestCase):
    async def test_decision_tick_ends_before_tool_finishes(self) -> None:
        captured: list[Any] = []
        _GateEffect.started = asyncio.Event()
        _GateEffect.release = asyncio.Event()
        registry = ToolRegistry()
        registry.register(_GateEffect)

        from tests.test_agent_loop_skeleton_contract import _factory_for, _values_of

        loop = AgentLoop(
            scope_key="group:1",
            planner=_GatePlanner(),
            session_factory=_factory_for(captured),
            tool_registry=registry,
        )
        with patch(
            "qqbot.services.agent_loop.loop._WAKE_BATCH_WINDOW_SECONDS", 0
        ):
            loop.start()
            loop.wake()
            await _GateEffect.started.wait()
            types = [_values_of(stmt).get("type") for stmt in captured]
            self.assertIn("runtime.tick_ended", types)
            ended = next(
                _values_of(stmt)
                for stmt in captured
                if _values_of(stmt).get("type") == "runtime.tick_ended"
            )
            self.assertEqual(ended["payload"]["program_status"], "dispatched")
            self.assertNotIn("agent.program_completed", types)
            _GateEffect.release.set()
            for _ in range(50):
                await asyncio.sleep(0.01)
                types = [_values_of(stmt).get("type") for stmt in captured]
                if "agent.program_completed" in types:
                    break
            await loop.stop()
        self.assertIn("agent.program_completed", types)


def _sent_text(message: Any) -> str:
    """从 send_all 交给 OneBot 的段数组里取回文字。

    2026-08-14 去协议化后 `send_all` 收的是**归一后的领域气泡**
    （`{"kind":"chat","text":…}`），段数组由 `build_chat_content` 在这一步才
    构造——所以断言只能对着构造出来的段看，不能再拿气泡里的字符串当哨兵。
    """
    if not isinstance(message, list):
        return str(message)
    for segment in message:
        if isinstance(segment, dict) and segment.get("type") == "text":
            return str((segment.get("data") or {}).get("text", ""))
    return ""


class SendSerializationTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_send_all_keeps_bubbles_contiguous(self) -> None:
        """并行程序同时发言时，一次调用的气泡不被另一段程序劈开。

        这把锁只保证连续性：不判重、不认领、不消费、不设 TTL。「要不要再说
        一次」仍由下一拍模型对着时间线判断（上下游边界契约 §4 不变）。
        """
        trace: list[str] = []

        async def fake_call_action(
            bot: Any, action: str, *, effect: bool, **params: Any
        ) -> tuple[None, None]:
            text = _sent_text(params.get("message"))
            trace.append(f"enter:{text}")
            await asyncio.sleep(0)  # 让出事件循环，无锁必然交错
            trace.append(f"exit:{text}")
            return None, None

        first = [{"kind": "chat", "text": "a1"}, {"kind": "chat", "text": "a2"}]
        second = [{"kind": "chat", "text": "b1"}, {"kind": "chat", "text": "b2"}]

        with patch(
            "qqbot.services.agent_loop.tools._onebot_common.call_action",
            new=fake_call_action,
        ):
            await asyncio.gather(
                send_all(object(), "group:1", first),
                send_all(object(), "group:1", second),
            )

        sent = [line.split(":", 1)[1] for line in trace if line.startswith("enter:")]
        self.assertEqual(len(sent), 4)
        self.assertIn(sent, (["a1", "a2", "b1", "b2"], ["b1", "b2", "a1", "a2"]))

    async def test_different_scopes_do_not_block_each_other(self) -> None:
        """互斥是 per-scope 的：别的群不该被这个群的发送挡住。"""
        entered = asyncio.Event()
        release = asyncio.Event()
        order: list[str] = []

        async def fake_call_action(
            bot: Any, action: str, *, effect: bool, **params: Any
        ) -> tuple[None, None]:
            text = _sent_text(params.get("message"))
            order.append(f"enter:{text}")
            if text == "slow":
                entered.set()
                await release.wait()
            return None, None

        with patch(
            "qqbot.services.agent_loop.tools._onebot_common.call_action",
            new=fake_call_action,
        ):
            slow = asyncio.create_task(
                send_all(object(), "group:1", [{"kind": "chat", "text": "slow"}])
            )
            await asyncio.wait_for(entered.wait(), timeout=1.0)
            await send_all(object(), "group:2", [{"kind": "chat", "text": "other"}])
            self.assertEqual(order, ["enter:slow", "enter:other"])
            release.set()
            await slow

    def tearDown(self) -> None:
        # 锁在 3.10+ 首次使用时绑定事件循环；每个用例一个新 loop，缓存必须清。
        from qqbot.services.agent_loop import outbound_messages

        outbound_messages._send_locks.clear()


if __name__ == "__main__":
    unittest.main()

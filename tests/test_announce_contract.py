"""`announce()` 合同——统一的 persist-then-notify 边界（2026-08-04）。

此前 `wait` 工具、SilenceWatcher、ReplyExecutor 各写一遍"写事实 + 叫醒"，
连"写失败还叫不叫醒"这种真语义都埋在各自的 try 里。收敛成一个函数之后，
差异只剩两个参数，本文件冻结的就是这两个参数的语义。

不碰 DB：用 `write_event=` 注入假写入器，因此全部是纯函数级断言。
"""

from __future__ import annotations

import unittest
from typing import Any

from qqbot.services.agent_loop.event_writer import (
    RuntimeEventPublisher,
    WakeMode,
    announce,
)


class _RecordingWriter:
    """假写入器：记录调用并返回固定 event_id；可配置成抛异常。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict] = []
        self._fail = fail

    async def __call__(self, session_factory: Any, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        if self._fail:
            raise RuntimeError("db down")
        return "evt-1"


class _RecordingWake:
    def __init__(self, *, fail: bool = False, order: list[str] | None = None) -> None:
        self.scopes: list[str] = []
        self._fail = fail
        self._order = order

    async def __call__(self, scope_key: str) -> None:
        if self._order is not None:
            self._order.append("wake")
        self.scopes.append(scope_key)
        if self._fail:
            raise RuntimeError("loop gone")


def _kwargs(**overrides: Any) -> dict:
    base = {
        "event_type": "runtime.wait_elapsed",
        "scope_key": "group:1",
        "visibility": "agent_visible",
        "correlation_id": "corr-1",
        "causation_id": None,
        "payload": {"seconds": 5},
    }
    base.update(overrides)
    return base


class AnnounceTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_visible_write_then_wake(self) -> None:
        writer, wake = _RecordingWriter(), _RecordingWake()
        event_id = await announce(
            None, **_kwargs(), wake=wake, write_event=writer
        )
        self.assertEqual(event_id, "evt-1")
        self.assertEqual(len(writer.calls), 1)
        self.assertEqual(wake.scopes, ["group:1"])

    async def test_wake_never_precedes_the_fact(self) -> None:
        """事件系统设计 §2：wake 不能领先于事实。

        否则被叫醒那一拍的投影读不到自己被叫醒的理由。
        """
        order: list[str] = []

        class _OrderedWriter(_RecordingWriter):
            async def __call__(self, session_factory: Any, **kwargs: Any) -> str:
                order.append("write")
                return await super().__call__(session_factory, **kwargs)

        await announce(
            None,
            **_kwargs(),
            wake=_RecordingWake(order=order),
            write_event=_OrderedWriter(),
        )
        self.assertEqual(order, ["write", "wake"])

    async def test_runtime_only_does_not_wake(self) -> None:
        """runtime_only 事实不进模型视野，为它开一拍没有意义。"""
        wake = _RecordingWake()
        await announce(
            None,
            **_kwargs(visibility="runtime_only"),
            wake=wake,
            write_event=_RecordingWriter(),
        )
        self.assertEqual(wake.scopes, [])

    async def test_no_wake_callback_writes_only(self) -> None:
        writer = _RecordingWriter()
        event_id = await announce(None, **_kwargs(), wake=None, write_event=writer)
        self.assertEqual(event_id, "evt-1")
        self.assertEqual(len(writer.calls), 1)

    async def test_write_failure_propagates_and_skips_wake_by_default(self) -> None:
        """reply 完成的语义：写失败就不叫。

        完成事实没落库却把她叫起来，她看不到等待结束，只会以为是别的事叫醒的。
        """
        wake = _RecordingWake()
        with self.assertRaises(RuntimeError):
            await announce(
                None,
                **_kwargs(),
                wake=wake,
                write_event=_RecordingWriter(fail=True),
            )
        self.assertEqual(wake.scopes, [])

    async def test_wake_on_write_failure_keeps_the_appointment(self) -> None:
        """`wait` / 静默到点的语义：本体是「到点我会来」的约定。

        事件只是醒来后的说明文字，宁可少一行说明，不可失约。
        """
        wake = _RecordingWake()
        event_id = await announce(
            None,
            **_kwargs(),
            wake=wake,
            wake_on_write_failure=True,
            write_event=_RecordingWriter(fail=True),
        )
        self.assertIsNone(event_id)
        self.assertEqual(wake.scopes, ["group:1"])

    async def test_wake_failure_never_becomes_a_write_failure(self) -> None:
        """事件已落库，叫醒失败不能把它反转成一次写失败。"""
        event_id = await announce(
            None,
            **_kwargs(),
            wake=_RecordingWake(fail=True),
            write_event=_RecordingWriter(),
        )
        self.assertEqual(event_id, "evt-1")


class RuntimeEventPublisherDelegationTests(unittest.IsolatedAsyncioTestCase):
    """publisher 退化成绑定配置的薄封装，语义必须与直接调 announce 等同。"""

    async def test_publish_writes_then_wakes(self) -> None:
        writer, wake = _RecordingWriter(), _RecordingWake()
        publisher = RuntimeEventPublisher(
            None, notify_event_available=wake, write_event=writer
        )
        event_id = await publisher.publish(**_kwargs())
        self.assertEqual(event_id, "evt-1")
        self.assertEqual(wake.scopes, ["group:1"])

    async def test_publish_defaults_to_not_waking_on_write_failure(self) -> None:
        wake = _RecordingWake()
        publisher = RuntimeEventPublisher(
            None,
            notify_event_available=wake,
            write_event=_RecordingWriter(fail=True),
        )
        with self.assertRaises(RuntimeError):
            await publisher.publish(**_kwargs())
        self.assertEqual(wake.scopes, [])


class WakeModeTests(unittest.TestCase):
    """三种模式必须都在，且互不相等——supervisor.wake 按它们分派。"""

    def test_three_modes_exist(self) -> None:
        self.assertEqual(
            {m.value for m in WakeMode},
            {"batched", "immediate", "immediate_no_arm"},
        )


if __name__ == "__main__":
    unittest.main()

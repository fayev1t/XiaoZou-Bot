"""静默门：登记之后才判定叫不叫醒。注册层不打 tag。"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from qqbot.services.event_gateway.silence_gate import (
    SILENCE_ELAPSED_TYPE,
    apply_silence_gate,
    decide_silence_gate,
    decide_silence_gate_for_event,
)


def _event(**overrides: object) -> SimpleNamespace:
    base: dict[str, object] = {
        "type": "external.message.group.normal",
        "visibility": "agent_visible",
        "scope": "group",
        "group_id": 9,
        "user_id": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class DecideSilenceGateTests(unittest.TestCase):
    def test_visible_group_message_wakes_and_notes(self) -> None:
        d = decide_silence_gate_for_event(_event())
        self.assertTrue(d.wake)
        self.assertTrue(d.note_activity)
        self.assertEqual(d.scope_key, "group:9")

    def test_runtime_only_does_not_wake(self) -> None:
        d = decide_silence_gate_for_event(
            _event(type="runtime.model_responded", visibility="runtime_only")
        )
        self.assertFalse(d.wake)
        self.assertFalse(d.note_activity)

    def test_silence_elapsed_wakes_without_noting(self) -> None:
        d = decide_silence_gate_for_event(
            _event(type=SILENCE_ELAPSED_TYPE)
        )
        self.assertTrue(d.wake)
        self.assertFalse(d.note_activity)

    def test_private_never_wakes(self) -> None:
        d = decide_silence_gate_for_event(
            _event(scope="private", group_id=None, user_id=1)
        )
        self.assertFalse(d.wake)
        self.assertFalse(d.note_activity)
        self.assertEqual(d.scope_key, "private:1")

    def test_system_visible_wakes(self) -> None:
        d = decide_silence_gate(
            event_type="external.meta.lifecycle",
            visibility="agent_visible",
            scope_key="system",
        )
        self.assertTrue(d.wake)
        self.assertTrue(d.note_activity)


class ApplySilenceGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_apply_order_is_note_then_wake(self) -> None:
        order: list[str] = []

        def note(scope_key: str) -> None:
            order.append(f"note:{scope_key}")

        async def wake(scope_key: str) -> None:
            order.append(f"wake:{scope_key}")

        await apply_silence_gate(_event(), wake=wake, note_activity=note)
        self.assertEqual(order, ["note:group:9", "wake:group:9"])

    async def test_runtime_only_apply_is_noop(self) -> None:
        woke: list[str] = []
        noted: list[str] = []

        async def wake(scope_key: str) -> None:
            woke.append(scope_key)

        await apply_silence_gate(
            _event(visibility="runtime_only"),
            wake=wake,
            note_activity=noted.append,
        )
        self.assertEqual(woke, [])
        self.assertEqual(noted, [])


if __name__ == "__main__":
    unittest.main()

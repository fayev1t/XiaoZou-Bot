"""Contract tests for the heartbeat bypass.

Verifies:
- serialize_heartbeat() shape matches the contract (EventIngest契约.md §7.1).
- write_heartbeat() is atomic via tempfile + os.replace.
- EventIngest.ingest() short-circuits heartbeat events without touching DB.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from qqbot.services.event_ingest import EventIngest
from qqbot.services.event_ingest.heartbeat import (
    HEARTBEAT_FILE,
    serialize_heartbeat,
    write_heartbeat,
)
from qqbot.services.event_ingest.mappers import build_default_registry


def _hb(**overrides: Any) -> SimpleNamespace:
    defaults = dict(
        post_type="meta_event",
        meta_event_type="heartbeat",
        time=1716700000,
        self_id=10000,
        interval=5000,
        status={"online": True, "good": True},
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class HeartbeatSerializationTests(unittest.TestCase):
    def test_payload_contains_contract_fields(self) -> None:
        payload = serialize_heartbeat(_hb())
        self.assertIn("self_id", payload)
        self.assertIn("last_heartbeat_at", payload)
        self.assertIn("interval_ms", payload)
        self.assertIn("status", payload)
        self.assertEqual(payload["self_id"], 10000)
        self.assertEqual(payload["interval_ms"], 5000)
        self.assertEqual(payload["status"], {"online": True, "good": True})

    def test_last_heartbeat_at_is_tz_aware_iso(self) -> None:
        payload = serialize_heartbeat(_hb())
        self.assertTrue(payload["last_heartbeat_at"].endswith("+08:00"))

    def test_pydantic_status_is_serialized_via_model_dump(self) -> None:
        class FakeStatus:
            def model_dump(self) -> dict:
                return {"good": False, "extra": 1}

        payload = serialize_heartbeat(_hb(status=FakeStatus()))
        self.assertEqual(payload["status"], {"good": False, "extra": 1})


class HeartbeatAtomicWriteTests(unittest.IsolatedAsyncioTestCase):
    async def test_write_creates_file_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            target = Path(tmp_dir) / "napcat_heartbeat.json"
            await write_heartbeat(_hb(), path=target)
            self.assertTrue(target.exists())
            data = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(data["self_id"], 10000)
            # No leftover temp files
            leftovers = [p for p in target.parent.iterdir() if p.name != target.name]
            self.assertEqual(leftovers, [])

    async def test_repeated_writes_only_keep_latest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            target = Path(tmp_dir) / "napcat_heartbeat.json"
            # 两次写不同时间戳，最后留下的应是较新的那一次（晚 5 秒）。
            # 用具体 epoch 验证字段内容，避免硬编码"今年"。
            # (1716700005 = 2024-05-26 13:06:45 +08:00)
            await write_heartbeat(_hb(time=1716700000), path=target)
            await write_heartbeat(_hb(time=1716700005), path=target)
            data = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(
                data["last_heartbeat_at"], "2024-05-26T13:06:45+08:00"
            )

    async def test_write_failure_is_swallowed(self) -> None:
        bad_path = Path("/nonexistent_root/napcat_heartbeat.json")
        # Should not raise even if the path is unwritable.
        await write_heartbeat(_hb(), path=bad_path)


class HeartbeatBypassedFromIngestTests(unittest.IsolatedAsyncioTestCase):
    async def test_heartbeat_does_not_touch_session(self) -> None:
        session_factory = MagicMock(
            side_effect=AssertionError("session must not be used for heartbeat")
        )
        ingest = EventIngest(build_default_registry(), session_factory=session_factory)
        with tempfile.TemporaryDirectory() as tmp_dir:
            target = Path(tmp_dir) / "napcat_heartbeat.json"
            # Inject the path via monkeypatching the module-level default.
            import qqbot.services.event_ingest.heartbeat as hb_mod

            original = hb_mod.HEARTBEAT_FILE
            hb_mod.HEARTBEAT_FILE = target
            try:
                result = await ingest.ingest(_hb())
            finally:
                hb_mod.HEARTBEAT_FILE = original

            self.assertEqual(result.status, "heartbeat")
            self.assertIsNone(result.event)
            self.assertTrue(target.exists())


class HeartbeatDefaultPathContractTests(unittest.TestCase):
    def test_default_path_lives_in_runtime_data(self) -> None:
        self.assertEqual(HEARTBEAT_FILE, Path("./runtime_data/napcat_heartbeat.json"))


if __name__ == "__main__":
    unittest.main()

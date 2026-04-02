from __future__ import annotations

import importlib.util
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TIME_FILE = ROOT / "qqbot" / "core" / "time.py"


def _load_time_module():
    spec = importlib.util.spec_from_file_location("test_time_module", TIME_FILE)
    if spec is None or spec.loader is None:
        raise AssertionError(f"failed to load time module from {TIME_FILE}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


time_module = _load_time_module()


class TimeContractTests(unittest.TestCase):
    def test_china_now_returns_timezone_aware_datetime(self) -> None:
        now = time_module.china_now()

        self.assertIsNotNone(now.tzinfo)
        self.assertEqual(now.tzinfo, time_module.CHINA_TIMEZONE)
        self.assertEqual(now.utcoffset(), timedelta(hours=8))

    def test_normalize_china_time_keeps_china_timezone(self) -> None:
        utc_value = datetime(2026, 4, 1, 0, 0, tzinfo=timezone.utc)
        normalized = time_module.normalize_china_time(utc_value)

        self.assertEqual(normalized.tzinfo, time_module.CHINA_TIMEZONE)
        self.assertEqual(normalized.hour, 8)

    def test_normalize_china_time_rejects_naive_datetime(self) -> None:
        with self.assertRaises(ValueError):
            time_module.normalize_china_time(datetime(2026, 4, 1, 8, 0))

    def test_models_and_scheduler_match_timezone_contract(self) -> None:
        messages_model = (ROOT / "qqbot" / "models" / "messages.py").read_text(encoding="utf-8")
        tool_call_model = (ROOT / "qqbot" / "models" / "tool_call.py").read_text(encoding="utf-8")
        scheduler = (ROOT / "qqbot" / "core" / "scheduler.py").read_text(encoding="utf-8")
        database = (ROOT / "qqbot" / "core" / "database.py").read_text(encoding="utf-8")
        converter = (ROOT / "qqbot" / "services" / "message_converter.py").read_text(encoding="utf-8")

        self.assertIn("DateTime(timezone=True)", messages_model)
        self.assertIn("DateTime(timezone=True)", tool_call_model)
        self.assertIn('server_default=text("CURRENT_TIMESTAMP")', tool_call_model)
        self.assertIn("scheduler.configure(timezone=CHINA_TIMEZONE)", scheduler)
        self.assertIn('"timezone": "Asia/Shanghai"', database)
        self.assertIn(
            "def _format_message_time(self, value: datetime | int | float | None) -> str:",
            converter,
        )
        self.assertNotIn("replace(tzinfo=None)", TIME_FILE.read_text(encoding="utf-8"))

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
        # v2 把 messages.py / tool_call.py / message_converter.py 全部删了
        # （[[v1-fully-discarded]]）；时区契约现在落在唯一的 agent_event 模型 +
        # scheduler + database 三处。
        agent_event_model = (
            ROOT / "qqbot" / "models" / "agent_event.py"
        ).read_text(encoding="utf-8")
        scheduler = (ROOT / "qqbot" / "core" / "scheduler.py").read_text(
            encoding="utf-8"
        )
        database = (ROOT / "qqbot" / "core" / "database.py").read_text(
            encoding="utf-8"
        )

        # 所有持久化时间戳必须 tz-aware
        self.assertIn("DateTime(timezone=True)", agent_event_model)
        # APScheduler 必须用 CHINA_TIMEZONE
        self.assertIn(
            "scheduler.configure(timezone=CHINA_TIMEZONE)", scheduler
        )
        # asyncpg connect_args 必须设 timezone=Asia/Shanghai
        self.assertIn('"timezone": "Asia/Shanghai"', database)
        # time.py 不应再剥离 tzinfo（防止 naive datetime 回流）
        self.assertNotIn(
            "replace(tzinfo=None)", TIME_FILE.read_text(encoding="utf-8")
        )

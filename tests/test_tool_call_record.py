from __future__ import annotations

import importlib
import sys
import types
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _install_tool_call_record_test_stubs() -> None:
    qqbot_package = types.ModuleType("qqbot")
    setattr(qqbot_package, "__path__", [str(ROOT / "qqbot")])
    sys.modules["qqbot"] = qqbot_package

    services_package = types.ModuleType("qqbot.services")
    setattr(services_package, "__path__", [str(ROOT / "qqbot" / "services")])
    sys.modules["qqbot.services"] = services_package

    models_package = types.ModuleType("qqbot.models")
    setattr(models_package, "__path__", [str(ROOT / "qqbot" / "models")])
    sys.modules["qqbot.models"] = models_package

    sqlalchemy_package = types.ModuleType("sqlalchemy")
    setattr(sqlalchemy_package, "__path__", [])
    setattr(sqlalchemy_package, "text", lambda sql: sql)
    setattr(sqlalchemy_package, "select", lambda *args, **kwargs: (args, kwargs))
    sys.modules["sqlalchemy"] = sqlalchemy_package

    sqlalchemy_ext_package = types.ModuleType("sqlalchemy.ext")
    setattr(sqlalchemy_ext_package, "__path__", [])
    sys.modules["sqlalchemy.ext"] = sqlalchemy_ext_package

    sqlalchemy_asyncio_module = types.ModuleType("sqlalchemy.ext.asyncio")

    class _DummyAsyncSession:
        pass

    setattr(sqlalchemy_asyncio_module, "AsyncSession", _DummyAsyncSession)
    sys.modules["sqlalchemy.ext.asyncio"] = sqlalchemy_asyncio_module

    time_module = types.ModuleType("qqbot.core.time")
    setattr(time_module, "china_now", lambda: "2026-04-03T00:00:00+08:00")
    sys.modules["qqbot.core.time"] = time_module

    tool_call_model_module = types.ModuleType("qqbot.models.tool_call")

    class FakeToolCallRecord:
        def __init__(self, **kwargs: object) -> None:
            for key, value in kwargs.items():
                setattr(self, key, value)

    setattr(tool_call_model_module, "ToolCallRecord", FakeToolCallRecord)
    sys.modules["qqbot.models.tool_call"] = tool_call_model_module


def _load_tool_call_record_module() -> Any:
    _install_tool_call_record_test_stubs()
    sys.modules.pop("qqbot.services.tool_call_record", None)
    return importlib.import_module("qqbot.services.tool_call_record")


class ToolCallRecordServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.module = _load_tool_call_record_module()

    async def test_create_record_persists_msg_hash(self) -> None:
        added_records: list[object] = []
        flush_calls: list[str] = []

        class FakeSession:
            def add(self, record: object) -> None:
                added_records.append(record)

            async def flush(self) -> None:
                flush_calls.append("flush")

        service = self.module.ToolCallRecordService(session=FakeSession())
        record = await service.create_record(
            call_hash="call-1",
            msg_hash="msg-1",
            tool_name="web_search",
            input_data="关键词",
            output_data="结果",
        )

        self.assertEqual(len(added_records), 1)
        self.assertEqual(flush_calls, ["flush"])
        self.assertEqual(record.msg_hash, "msg-1")
        self.assertEqual(record.call_hash, "call-1")
        self.assertEqual(record.tool_name, "web_search")


if __name__ == "__main__":
    unittest.main()

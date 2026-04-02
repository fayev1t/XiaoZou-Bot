from __future__ import annotations

import importlib
import sys
import types
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _install_tool_manager_test_stubs() -> None:
    qqbot_package = types.ModuleType("qqbot")
    setattr(qqbot_package, "__path__", [str(ROOT / "qqbot")])
    sys.modules["qqbot"] = qqbot_package

    services_package = types.ModuleType("qqbot.services")
    setattr(services_package, "__path__", [str(ROOT / "qqbot" / "services")])
    sys.modules["qqbot.services"] = services_package

    core_package = types.ModuleType("qqbot.core")
    setattr(core_package, "__path__", [str(ROOT / "qqbot" / "core")])
    sys.modules["qqbot.core"] = core_package

    sqlalchemy_package = types.ModuleType("sqlalchemy")
    setattr(sqlalchemy_package, "__path__", [])
    sys.modules["sqlalchemy"] = sqlalchemy_package

    sqlalchemy_ext_package = types.ModuleType("sqlalchemy.ext")
    setattr(sqlalchemy_ext_package, "__path__", [])
    sys.modules["sqlalchemy.ext"] = sqlalchemy_ext_package

    sqlalchemy_asyncio_module = types.ModuleType("sqlalchemy.ext.asyncio")

    class _DummyAsyncSession:
        pass

    setattr(sqlalchemy_asyncio_module, "AsyncSession", _DummyAsyncSession)
    sys.modules["sqlalchemy.ext.asyncio"] = sqlalchemy_asyncio_module

    ids_module = types.ModuleType("qqbot.core.ids")
    setattr(ids_module, "new_call_hash", lambda: "call-new")
    sys.modules["qqbot.core.ids"] = ids_module

    tool_call_record_module = types.ModuleType("qqbot.services.tool_call_record")
    setattr(tool_call_record_module, "ToolCallRecordService", object)
    setattr(tool_call_record_module, "build_system_tool_call_xml", lambda **kwargs: "xml")
    sys.modules["qqbot.services.tool_call_record"] = tool_call_record_module

    web_search_module = types.ModuleType("qqbot.services.web_search")
    setattr(web_search_module, "WebSearchService", object)
    sys.modules["qqbot.services.web_search"] = web_search_module


def _load_tool_manager_module() -> Any:
    _install_tool_manager_test_stubs()
    sys.modules.pop("qqbot.services.tool_manager", None)
    return importlib.import_module("qqbot.services.tool_manager")


class ToolManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.module = _load_tool_manager_module()

    async def test_execute_image_parse_reuses_existing_record(self) -> None:
        create_calls: list[dict[str, object]] = []

        class FakeRecord:
            call_hash = "call-old"
            tool_name = "image_parse"
            input_data = "hash-a"
            output_data = "旧结果"

        class FakeRecordService:
            def __init__(self, session: object) -> None:
                _ = session

            async def get_reusable_record(self, **kwargs: object) -> FakeRecord | None:
                _ = kwargs
                return FakeRecord()

            async def create_record(self, **kwargs: object) -> None:
                create_calls.append(dict(kwargs))

        class FakeWebSearchService:
            pass

        self.module.ToolCallRecordService = FakeRecordService
        self.module.WebSearchService = FakeWebSearchService

        manager = self.module.ToolManager(session=object())
        result = await manager.execute_image_parse(
            msg_hash="msg-1",
            file_hash="hash-a",
            generator=lambda: self.fail("should not regenerate cached image parse"),
        )

        self.assertEqual(result.call_hash, "call-new")
        self.assertFalse(result.is_newly_generated)
        self.assertEqual(
            create_calls,
            [
                {
                    "call_hash": "call-new",
                    "msg_hash": "msg-1",
                    "tool_name": "image_parse",
                    "input_data": "hash-a",
                    "output_data": "旧结果",
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()

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

    logging_module = types.ModuleType("qqbot.core.logging")

    class _DummyLogger:
        def info(self, *args: object, **kwargs: object) -> None:
            _ = args, kwargs

        def warning(self, *args: object, **kwargs: object) -> None:
            _ = args, kwargs

    setattr(logging_module, "get_logger", lambda _name: _DummyLogger())
    sys.modules["qqbot.core.logging"] = logging_module

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

    async def test_execute_web_search_keeps_public_tool_name(self) -> None:
        create_calls: list[dict[str, object]] = []
        search_calls: list[str] = []

        class FakeRecordService:
            def __init__(self, session: object) -> None:
                _ = session

            async def create_record(self, **kwargs: object) -> None:
                create_calls.append(dict(kwargs))

        class FakeWebSearchService:
            async def search(self, query: str) -> str:
                search_calls.append(query)
                return "搜索结果"

            async def crawl(self, url: str) -> str:
                _ = url
                raise AssertionError("web_search 不应调用 crawl")

        self.module.ToolCallRecordService = FakeRecordService
        self.module.WebSearchService = FakeWebSearchService

        manager = self.module.ToolManager(session=object())
        result = await manager.execute_web_search(msg_hash="msg-1", query="查询词")

        self.assertEqual(search_calls, ["查询词"])
        self.assertEqual(result.tool_name, "web_search")
        self.assertEqual(result.output_data, "搜索结果")
        self.assertTrue(result.is_newly_generated)
        self.assertEqual(create_calls[0]["tool_name"], "web_search")
        self.assertEqual(create_calls[0]["input_data"], "查询词")

    async def test_execute_image_parse_can_disable_reuse_for_contextual_reparse(self) -> None:
        create_calls: list[dict[str, object]] = []
        reusable_lookup_calls: list[dict[str, object]] = []
        generator_calls: list[str] = []

        class FakeRecordService:
            def __init__(self, session: object) -> None:
                _ = session

            async def get_reusable_record(self, **kwargs: object) -> None:
                reusable_lookup_calls.append(dict(kwargs))
                return None

            async def create_record(self, **kwargs: object) -> None:
                create_calls.append(dict(kwargs))

        class FakeWebSearchService:
            pass

        self.module.ToolCallRecordService = FakeRecordService
        self.module.WebSearchService = FakeWebSearchService

        async def fake_generator() -> str:
            generator_calls.append("called")
            return "新图像描述"

        manager = self.module.ToolManager(session=object())
        result = await manager.execute_image_parse(
            msg_hash="msg-ctx",
            file_hash="hash-ctx",
            generator=fake_generator,
            reuse_existing=False,
        )

        self.assertEqual(reusable_lookup_calls, [])
        self.assertEqual(generator_calls, ["called"])
        self.assertTrue(result.is_newly_generated)
        self.assertEqual(result.output_data, "新图像描述")
        self.assertEqual(create_calls[0]["msg_hash"], "msg-ctx")
        self.assertEqual(create_calls[0]["input_data"], "hash-ctx")

    async def test_execute_web_crawl_keeps_public_tool_name(self) -> None:
        create_calls: list[dict[str, object]] = []
        crawl_calls: list[str] = []

        class FakeRecordService:
            def __init__(self, session: object) -> None:
                _ = session

            async def create_record(self, **kwargs: object) -> None:
                create_calls.append(dict(kwargs))

        class FakeWebSearchService:
            async def search(self, query: str) -> str:
                _ = query
                raise AssertionError("web_crawl 不应调用 search")

            async def crawl(self, url: str) -> str:
                crawl_calls.append(url)
                return "抓取结果"

        self.module.ToolCallRecordService = FakeRecordService
        self.module.WebSearchService = FakeWebSearchService

        manager = self.module.ToolManager(session=object())
        result = await manager.execute_web_crawl(msg_hash="msg-2", url="https://example.com")

        self.assertEqual(crawl_calls, ["https://example.com"])
        self.assertEqual(result.tool_name, "web_crawl")
        self.assertEqual(result.output_data, "抓取结果")
        self.assertTrue(result.is_newly_generated)
        self.assertEqual(create_calls[0]["tool_name"], "web_crawl")
        self.assertEqual(create_calls[0]["input_data"], "https://example.com")

if __name__ == "__main__":
    unittest.main()

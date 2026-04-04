from __future__ import annotations

import hashlib
import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _install_image_parsing_test_stubs() -> None:
    class _DummyLogger:
        def info(self, *args: object, **kwargs: object) -> None:
            _ = args, kwargs

        def warning(self, *args: object, **kwargs: object) -> None:
            _ = args, kwargs

        def error(self, *args: object, **kwargs: object) -> None:
            _ = args, kwargs

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
    setattr(logging_module, "get_logger", lambda _name: _DummyLogger())
    sys.modules["qqbot.core.logging"] = logging_module

    llm_module = types.ModuleType("qqbot.core.llm")

    async def _dummy_create_llm(*args: object, **kwargs: object) -> None:
        _ = args, kwargs
        return None

    setattr(llm_module, "create_llm", _dummy_create_llm)
    sys.modules["qqbot.core.llm"] = llm_module

    tool_manager_module = types.ModuleType("qqbot.services.tool_manager")

    class ToolCallResult:
        pass

    class ToolManager:
        def __init__(self, session: object | None) -> None:
            self.session = session

    setattr(tool_manager_module, "ToolCallResult", ToolCallResult)
    setattr(tool_manager_module, "ToolManager", ToolManager)
    sys.modules["qqbot.services.tool_manager"] = tool_manager_module

    httpx_module = types.ModuleType("httpx")

    class AsyncClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            _ = args, kwargs

        async def __aenter__(self) -> "AsyncClient":
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            _ = exc_type, exc, tb

        async def get(self, url: str) -> object:
            raise AssertionError(f"unexpected httpx request: {url}")

    setattr(httpx_module, "AsyncClient", AsyncClient)
    sys.modules["httpx"] = httpx_module

    langchain_messages_module = types.ModuleType("langchain_core.messages")

    class HumanMessage:
        def __init__(self, content: object) -> None:
            self.content = content

    setattr(langchain_messages_module, "HumanMessage", HumanMessage)
    sys.modules["langchain_core.messages"] = langchain_messages_module

    sqlalchemy_package = types.ModuleType("sqlalchemy")
    setattr(sqlalchemy_package, "__path__", [])
    sys.modules["sqlalchemy"] = sqlalchemy_package

    sqlalchemy_ext_package = types.ModuleType("sqlalchemy.ext")
    setattr(sqlalchemy_ext_package, "__path__", [])
    sys.modules["sqlalchemy.ext"] = sqlalchemy_ext_package

    sqlalchemy_asyncio_module = types.ModuleType("sqlalchemy.ext.asyncio")

    class AsyncSession:
        pass

    setattr(sqlalchemy_asyncio_module, "AsyncSession", AsyncSession)
    sys.modules["sqlalchemy.ext.asyncio"] = sqlalchemy_asyncio_module

    nonebot_package = types.ModuleType("nonebot")
    setattr(nonebot_package, "__path__", [])
    sys.modules["nonebot"] = nonebot_package

    adapters_package = types.ModuleType("nonebot.adapters")
    setattr(adapters_package, "__path__", [])
    sys.modules["nonebot.adapters"] = adapters_package

    onebot_package = types.ModuleType("nonebot.adapters.onebot")
    setattr(onebot_package, "__path__", [])
    sys.modules["nonebot.adapters.onebot"] = onebot_package

    onebot_v11_module = types.ModuleType("nonebot.adapters.onebot.v11")

    class MessageSegment:
        pass

    setattr(onebot_v11_module, "MessageSegment", MessageSegment)
    sys.modules["nonebot.adapters.onebot.v11"] = onebot_v11_module


def _load_image_parsing_module() -> Any:
    _install_image_parsing_test_stubs()
    sys.modules.pop("qqbot.services.image_parsing", None)
    return importlib.import_module("qqbot.services.image_parsing")


class _FakeResponse:
    def __init__(self, content: bytes) -> None:
        self.content = content

    def raise_for_status(self) -> None:
        return None


class ImageParsingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.module = _load_image_parsing_module()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(self._cleanup_temp_dir)
        self.module.IMAGE_CACHE_DIR = Path(self.temp_dir.name)

    async def _cleanup_temp_dir(self) -> None:
        self.temp_dir.cleanup()

    async def test_parse_segment_sync_generates_initial_description_and_caches_image(self) -> None:
        image_bytes = b"\x89PNG\r\n\x1a\nimage-bytes"

        class FakeAsyncClient:
            def __init__(self, *args: object, **kwargs: object) -> None:
                _ = args, kwargs

            async def __aenter__(self) -> "FakeAsyncClient":
                return self

            async def __aexit__(self, exc_type, exc, tb) -> None:
                _ = exc_type, exc, tb

            async def get(self, url: str) -> _FakeResponse:
                self.last_url = url
                return _FakeResponse(image_bytes)

        self.module.httpx.AsyncClient = FakeAsyncClient

        segment = types.SimpleNamespace(
            type="image",
            data={"url": "https://example.com/image.png"},
        )

        service = self.module.ImageParsingService()

        async def _fake_generate_initial_image_summary(_service, data: bytes) -> str:
            _ = data
            return "一只坐着的猫"

        service._generate_initial_image_summary = types.MethodType(
            _fake_generate_initial_image_summary,
            service,
        )
        result = await service.parse_segment_sync(
            group_id=10001,
            segment=segment,
            msg_hash="msg-1",
        )

        expected_hash = hashlib.md5(image_bytes).hexdigest()
        self.assertEqual(result.file_hash, expected_hash)
        self.assertEqual(result.description, "一只坐着的猫")
        self.assertTrue((self.module.IMAGE_CACHE_DIR / f"{expected_hash}.png").exists())

    async def test_build_layer3_image_inputs_reads_cached_image_as_data_url(self) -> None:
        image_bytes = b"\x89PNG\r\n\x1a\nimage-for-layer3"
        file_hash = hashlib.md5(image_bytes).hexdigest()
        cached_file = self.module.IMAGE_CACHE_DIR / f"{file_hash}.png"
        cached_file.parent.mkdir(parents=True, exist_ok=True)
        cached_file.write_bytes(image_bytes)

        service = self.module.ImageParsingService()
        image_inputs = service.build_layer3_image_inputs([file_hash, file_hash, "missing"])

        self.assertEqual(len(image_inputs), 2)
        self.assertEqual(image_inputs[0]["type"], "text")
        self.assertIn(file_hash, image_inputs[0]["text"])
        self.assertEqual(image_inputs[1]["type"], "image_url")
        self.assertTrue(
            image_inputs[1]["image_url"]["url"].startswith("data:image/png;base64,")
        )

    async def test_parse_segment_sync_keeps_real_hash_when_initial_description_fails(self) -> None:
        image_bytes = b"\x89PNG\r\n\x1a\nimage-bytes-2"

        class FakeAsyncClient:
            def __init__(self, *args: object, **kwargs: object) -> None:
                _ = args, kwargs

            async def __aenter__(self) -> "FakeAsyncClient":
                return self

            async def __aexit__(self, exc_type, exc, tb) -> None:
                _ = exc_type, exc, tb

            async def get(self, url: str) -> _FakeResponse:
                _ = url
                return _FakeResponse(image_bytes)

        self.module.httpx.AsyncClient = FakeAsyncClient

        segment = types.SimpleNamespace(
            type="image",
            data={"url": "https://example.com/image-2.png"},
        )

        service = self.module.ImageParsingService()

        async def _failing_generate_initial_image_summary(_service, data: bytes) -> str:
            _ = data
            raise RuntimeError("vision unavailable")

        service._generate_initial_image_summary = types.MethodType(
            _failing_generate_initial_image_summary,
            service,
        )
        result = await service.parse_segment_sync(
            group_id=10001,
            segment=segment,
            msg_hash="msg-2",
        )

        expected_hash = hashlib.md5(image_bytes).hexdigest()
        self.assertEqual(result.file_hash, expected_hash)
        self.assertEqual(result.description, self.module.DEFAULT_FAILURE_DESC)
        self.assertTrue((self.module.IMAGE_CACHE_DIR / f"{expected_hash}.png").exists())


if __name__ == "__main__":
    unittest.main()

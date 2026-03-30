from __future__ import annotations

import base64
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
        def debug(self, *args: object, **kwargs: object) -> None:
            _ = args, kwargs

        def info(self, *args: object, **kwargs: object) -> None:
            _ = args, kwargs

        def warning(self, *args: object, **kwargs: object) -> None:
            _ = args, kwargs

        def error(self, *args: object, **kwargs: object) -> None:
            _ = args, kwargs

    qqbot_package = types.ModuleType("qqbot")
    setattr(qqbot_package, "__path__", [str(ROOT / "qqbot")])
    sys.modules["qqbot"] = qqbot_package

    core_package = types.ModuleType("qqbot.core")
    setattr(core_package, "__path__", [str(ROOT / "qqbot" / "core")])
    sys.modules["qqbot.core"] = core_package

    services_package = types.ModuleType("qqbot.services")
    setattr(services_package, "__path__", [str(ROOT / "qqbot" / "services")])
    sys.modules["qqbot.services"] = services_package

    logging_module = types.ModuleType("qqbot.core.logging")
    setattr(logging_module, "get_logger", lambda name: _DummyLogger())
    sys.modules["qqbot.core.logging"] = logging_module

    llm_module = types.ModuleType("qqbot.core.llm")

    async def _dummy_create_llm(*args: object, **kwargs: object) -> None:
        _ = args, kwargs
        return None

    setattr(llm_module, "create_llm", _dummy_create_llm)
    sys.modules["qqbot.core.llm"] = llm_module

    httpx_module = types.ModuleType("httpx")

    class _DummyAsyncClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            _ = args, kwargs

        async def __aenter__(self) -> "_DummyAsyncClient":
            return self

        async def __aexit__(
            self,
            exc_type: object,
            exc: object,
            tb: object,
        ) -> None:
            _ = exc_type, exc, tb

    setattr(httpx_module, "AsyncClient", _DummyAsyncClient)
    sys.modules["httpx"] = httpx_module

    group_message_module = types.ModuleType("qqbot.services.group_message")

    class _DummyGroupMessageService:
        def __init__(self, session: object) -> None:
            self.session = session

    setattr(group_message_module, "GroupMessageService", _DummyGroupMessageService)
    sys.modules["qqbot.services.group_message"] = group_message_module

    image_record_module = types.ModuleType("qqbot.services.image_record")

    class _DummyImageRecordService:
        def __init__(self, session: object) -> None:
            self.session = session

        async def get_by_hashes(self, file_hashes: list[str]) -> dict[str, object]:
            _ = file_hashes
            return {}

    setattr(image_record_module, "ImageRecordService", _DummyImageRecordService)
    sys.modules["qqbot.services.image_record"] = image_record_module

    messages_module = types.ModuleType("langchain_core.messages")

    class _DummyHumanMessage:
        def __init__(self, content: object) -> None:
            self.content = content

    setattr(messages_module, "HumanMessage", _DummyHumanMessage)
    sys.modules["langchain_core.messages"] = messages_module

    nonebot_module = types.ModuleType("nonebot")
    adapters_module = types.ModuleType("nonebot.adapters")
    onebot_module = types.ModuleType("nonebot.adapters.onebot")
    onebot_v11_module = types.ModuleType("nonebot.adapters.onebot.v11")

    class _DummyMessageSegment:
        def __init__(self, type: str = "image", data: dict[str, object] | None = None) -> None:
            self.type = type
            self.data = data or {}

    setattr(onebot_v11_module, "MessageSegment", _DummyMessageSegment)
    sys.modules["nonebot"] = nonebot_module
    sys.modules["nonebot.adapters"] = adapters_module
    sys.modules["nonebot.adapters.onebot"] = onebot_module
    sys.modules["nonebot.adapters.onebot.v11"] = onebot_v11_module

    sqlalchemy_module = types.ModuleType("sqlalchemy")
    sqlalchemy_ext_module = types.ModuleType("sqlalchemy.ext")
    sqlalchemy_asyncio_module = types.ModuleType("sqlalchemy.ext.asyncio")

    class _DummyAsyncSession:
        pass

    setattr(sqlalchemy_asyncio_module, "AsyncSession", _DummyAsyncSession)
    sys.modules["sqlalchemy"] = sqlalchemy_module
    sys.modules["sqlalchemy.ext"] = sqlalchemy_ext_module
    sys.modules["sqlalchemy.ext.asyncio"] = sqlalchemy_asyncio_module


def _load_image_parsing_module() -> Any:
    _install_image_parsing_test_stubs()
    sys.modules.pop("qqbot.services.image_parsing", None)
    return importlib.import_module("qqbot.services.image_parsing")


image_parsing = _load_image_parsing_module()


class _FakeRecord:
    def __init__(self, local_path: str) -> None:
        self.local_path = local_path


class _FakeRecordService:
    def __init__(self, records: dict[str, _FakeRecord]) -> None:
        self._records = records

    async def get_by_hashes(self, file_hashes: list[str]) -> dict[str, _FakeRecord]:
        return {
            file_hash: self._records[file_hash]
            for file_hash in file_hashes
            if file_hash in self._records
        }


class ImageParsingPayloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_build_openai_image_blocks_interleaves_hash_hints_and_images(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            first_path = temp_path / "hash-a.jpg"
            second_path = temp_path / "hash-b.png"
            first_bytes = b"\xff\xd8\xfffake-jpeg"
            second_bytes = b"\x89PNG\r\n\x1a\nfake-png"
            first_path.write_bytes(first_bytes)
            second_path.write_bytes(second_bytes)

            service = image_parsing.ImageParsingService(session=None)
            service.record_service = _FakeRecordService(
                {
                    "hash-b": _FakeRecord(local_path=second_path.as_posix()),
                    "hash-a": _FakeRecord(local_path=first_path.as_posix()),
                }
            )

            refs = [
                image_parsing.ImageReference(file_hash="hash-b"),
                image_parsing.ImageReference(file_hash="hash-a"),
                image_parsing.ImageReference(file_hash="hash-b"),
            ]

            original_to_thread = image_parsing.asyncio.to_thread

            async def _immediate_to_thread(func: Any, /, *args: Any, **kwargs: Any) -> Any:
                return func(*args, **kwargs)

            image_parsing.asyncio.to_thread = _immediate_to_thread
            try:
                blocks = await service.build_openai_image_blocks(refs)
            finally:
                image_parsing.asyncio.to_thread = original_to_thread

        self.assertEqual(
            [block.get("type") for block in blocks],
            ["text", "image_url", "text", "image_url"],
        )
        self.assertEqual(
            [blocks[0]["text"], blocks[2]["text"]],
            [
                "以下图片对应 file_hash=hash-b。请将它与当前对话上下文中相同 file_hash 的 System-Image 线索对应理解。",
                "以下图片对应 file_hash=hash-a。请将它与当前对话上下文中相同 file_hash 的 System-Image 线索对应理解。",
            ],
        )
        self.assertNotIn("第2条消息", blocks[0]["text"])
        self.assertNotIn("第5条消息", blocks[2]["text"])

        expected_png = "data:image/png;base64," + base64.b64encode(second_bytes).decode(
            "utf-8"
        )
        expected_jpg = "data:image/jpeg;base64," + base64.b64encode(first_bytes).decode(
            "utf-8"
        )
        self.assertEqual(blocks[1]["image_url"]["url"], expected_png)
        self.assertEqual(blocks[3]["image_url"]["url"], expected_jpg)


if __name__ == "__main__":
    unittest.main()

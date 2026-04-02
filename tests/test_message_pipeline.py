from __future__ import annotations

import importlib
import sys
import types
import unittest
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _install_message_pipeline_test_stubs() -> None:
    qqbot_package = types.ModuleType("qqbot")
    setattr(qqbot_package, "__path__", [str(ROOT / "qqbot")])
    sys.modules["qqbot"] = qqbot_package

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

    class _DummyGroupMessageEvent:
        pass

    setattr(onebot_v11_module, "GroupMessageEvent", _DummyGroupMessageEvent)
    sys.modules["nonebot.adapters.onebot.v11"] = onebot_v11_module

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

    services_package = types.ModuleType("qqbot.services")
    setattr(services_package, "__path__", [str(ROOT / "qqbot" / "services")])
    sys.modules["qqbot.services"] = services_package

    core_package = types.ModuleType("qqbot.core")
    setattr(core_package, "__path__", [str(ROOT / "qqbot" / "core")])
    sys.modules["qqbot.core"] = core_package

    time_module = types.ModuleType("qqbot.core.time")
    setattr(time_module, "normalize_china_time", lambda value: value)
    sys.modules["qqbot.core.time"] = time_module

    ids_module = types.ModuleType("qqbot.core.ids")
    setattr(ids_module, "new_msg_hash", lambda: "msg-hash-123")
    sys.modules["qqbot.core.ids"] = ids_module

    group_module = types.ModuleType("qqbot.services.group")
    setattr(group_module, "GroupService", object)
    sys.modules["qqbot.services.group"] = group_module

    group_message_module = types.ModuleType("qqbot.services.group_message")
    setattr(group_message_module, "GroupMessageService", object)
    sys.modules["qqbot.services.group_message"] = group_message_module

    message_converter_module = types.ModuleType("qqbot.services.message_converter")
    setattr(message_converter_module, "MessageConverter", object)
    sys.modules["qqbot.services.message_converter"] = message_converter_module

    user_module = types.ModuleType("qqbot.services.user")
    setattr(user_module, "UserService", object)
    sys.modules["qqbot.services.user"] = user_module


def _load_message_pipeline_module() -> Any:
    _install_message_pipeline_test_stubs()
    sys.modules.pop("qqbot.services.message_pipeline", None)
    return importlib.import_module("qqbot.services.message_pipeline")


@dataclass
class FakeConversion:
    content: str
    message_type: str


@dataclass
class FakeEvent:
    group_id: int = 10001
    user_id: int = 20002
    message_id: int = 30003
    time: datetime = datetime(2026, 4, 2, 12, 0, 0)
    original_message: str | None = None
    raw_message: str = "原始消息"
    message: str = "消息对象"


class MessagePipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.module = _load_message_pipeline_module()

    def test_extract_raw_prefers_original_message(self) -> None:
        pipeline = self.module.MessagePipeline(converter=object())
        event = FakeEvent(original_message="原始优先", raw_message="raw", message="msg")

        self.assertEqual(pipeline.extract_raw(event), "原始优先")

    async def test_persist_raw_saves_null_formatted_message(self) -> None:
        saved_payloads: list[dict[str, object]] = []

        class FakeUserService:
            def __init__(self, session: object) -> None:
                _ = session

            async def get_or_create_user(self, user_id: int) -> None:
                _ = user_id

        class FakeGroupService:
            def __init__(self, session: object) -> None:
                _ = session

            async def get_or_create_group(self, group_id: int) -> None:
                _ = group_id

        class FakeGroupMessageService:
            def __init__(self, session: object) -> None:
                _ = session

            async def save_message(self, **kwargs: object) -> int:
                saved_payloads.append(kwargs)
                return 123

        self.module.UserService = FakeUserService
        self.module.GroupService = FakeGroupService
        self.module.GroupMessageService = FakeGroupMessageService

        pipeline = self.module.MessagePipeline(converter=object())
        record = pipeline.create_raw_record(FakeEvent(), "只存原文")
        saved_id = await pipeline.persist_raw(object(), record)

        self.assertEqual(saved_id, 123)
        self.assertEqual(len(saved_payloads), 1)
        self.assertEqual(saved_payloads[0]["msg_hash"], "msg-hash-123")
        self.assertEqual(saved_payloads[0]["raw_message"], "只存原文")
        self.assertIsNone(saved_payloads[0]["formatted_message"])

    async def test_format_and_update_updates_existing_row(self) -> None:
        update_calls: list[dict[str, object]] = []
        received_msg_hashes: list[str] = []

        class FakeConverter:
            async def convert_event(self, session: object, event: object, msg_hash: str) -> FakeConversion:
                _ = session, event
                received_msg_hashes.append(msg_hash)
                return FakeConversion(content="<System-Message>格式化</System-Message>", message_type="xml")

        class FakeGroupMessageService:
            def __init__(self, session: object) -> None:
                _ = session

            async def update_formatted_message(self, **kwargs: object) -> None:
                update_calls.append(kwargs)

        self.module.GroupMessageService = FakeGroupMessageService

        pipeline = self.module.MessagePipeline(converter=FakeConverter())
        record = await pipeline.format_and_update(
            object(),
            FakeEvent(),
            saved_id=77,
            msg_hash="msg-hash-xyz",
            raw_message="原文",
        )

        self.assertEqual(record.raw_message, "原文")
        self.assertEqual(record.msg_hash, "msg-hash-xyz")
        self.assertEqual(record.formatted_message, "<System-Message>格式化</System-Message>")
        self.assertEqual(record.message_type, "xml")
        self.assertEqual(received_msg_hashes, ["msg-hash-xyz"])
        self.assertEqual(
            update_calls,
            [
                {
                    "group_id": 10001,
                    "local_message_id": 77,
                    "formatted_message": "<System-Message>格式化</System-Message>",
                }
            ],
        )

    async def test_format_and_update_writes_parse_failed_placeholder_when_converter_raises(self) -> None:
        update_calls: list[dict[str, object]] = []

        class FailingConverter:
            async def convert_event(self, session: object, event: object, msg_hash: str) -> FakeConversion:
                _ = session, event, msg_hash
                raise RuntimeError("boom")

        class FakeGroupMessageService:
            def __init__(self, session: object) -> None:
                _ = session

            async def update_formatted_message(self, **kwargs: object) -> None:
                update_calls.append(kwargs)

        self.module.GroupMessageService = FakeGroupMessageService

        pipeline = self.module.MessagePipeline(converter=FailingConverter())
        record = await pipeline.format_and_update(
            object(),
            FakeEvent(),
            saved_id=88,
            msg_hash="msg-hash-failed",
            raw_message="[CQ:image,file=test.jpg]",
        )

        self.assertEqual(record.raw_message, "[CQ:image,file=test.jpg]")
        self.assertEqual(record.msg_hash, "msg-hash-failed")
        self.assertEqual(record.message_type, "unknown")
        self.assertIn("<System-Message", record.formatted_message)
        self.assertIn("【解析失败：消息格式化异常】", record.formatted_message)
        self.assertNotIn("[CQ:image,file=test.jpg]", record.formatted_message)
        self.assertEqual(len(update_calls), 1)
        self.assertEqual(update_calls[0]["group_id"], 10001)
        self.assertEqual(update_calls[0]["local_message_id"], 88)
        self.assertIn("【解析失败：消息格式化异常】", str(update_calls[0]["formatted_message"]))


if __name__ == "__main__":
    unittest.main()

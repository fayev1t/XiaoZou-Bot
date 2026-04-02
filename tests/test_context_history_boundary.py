from __future__ import annotations

import importlib
import sys
import types
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _install_context_test_stubs() -> None:
    qqbot_package = types.ModuleType("qqbot")
    setattr(qqbot_package, "__path__", [str(ROOT / "qqbot")])
    sys.modules["qqbot"] = qqbot_package

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

    onebot_module = types.ModuleType("nonebot.adapters.onebot.v11")

    class _DummyMessage(str):
        pass

    setattr(onebot_module, "Message", _DummyMessage)
    sys.modules["nonebot.adapters.onebot.v11"] = onebot_module

    settings_module = types.ModuleType("qqbot.core.settings")
    setattr(settings_module, "get_primary_bot_name", lambda: "小奏")
    sys.modules["qqbot.core.settings"] = settings_module

    group_member_module = types.ModuleType("qqbot.services.group_member")
    setattr(group_member_module, "GroupMemberService", object)
    sys.modules["qqbot.services.group_member"] = group_member_module

    group_message_module = types.ModuleType("qqbot.services.group_message")
    setattr(group_message_module, "GroupMessageService", object)
    sys.modules["qqbot.services.group_message"] = group_message_module

    message_converter_module = types.ModuleType("qqbot.services.message_converter")
    setattr(message_converter_module, "MessageConverter", object)
    sys.modules["qqbot.services.message_converter"] = message_converter_module

    tool_call_record_module = types.ModuleType("qqbot.services.tool_call_record")
    setattr(tool_call_record_module, "ToolCallRecordService", object)
    setattr(tool_call_record_module, "build_system_tool_call_xml", lambda **kwargs: "")
    sys.modules["qqbot.services.tool_call_record"] = tool_call_record_module

    user_module = types.ModuleType("qqbot.services.user")
    setattr(user_module, "UserService", object)
    sys.modules["qqbot.services.user"] = user_module


def _load_context_module() -> Any:
    _install_context_test_stubs()
    sys.modules.pop("qqbot.services.context", None)
    return importlib.import_module("qqbot.services.context")


class ContextHistoryBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.module = _load_context_module()

    async def test_get_recent_context_forwards_before_message_id(self) -> None:
        recent_message_calls: list[dict[str, object]] = []

        class FakeGroupMessageService:
            def __init__(self, session: object) -> None:
                _ = session

            async def get_recent_messages(self, **kwargs: object) -> list[dict[str, object]]:
                recent_message_calls.append(kwargs)
                return []

        class FakeToolCallRecordService:
            def __init__(self, session: object) -> None:
                _ = session

            async def get_records_by_msg_hashes(self, msg_hashes: list[str]) -> dict[str, list[object]]:
                _ = msg_hashes
                return {}

        class FakeMessageConverter:
            def wrap_plain_text(self, *args: object, **kwargs: object) -> str:
                _ = args, kwargs
                return ""

        self.module.GroupMessageService = FakeGroupMessageService
        self.module.MessageConverter = FakeMessageConverter
        self.module.ToolCallRecordService = FakeToolCallRecordService

        manager = self.module.ContextManager(session=object())
        context = await manager.get_recent_context(
            group_id=123,
            limit=50,
            bot_id=456,
            before_message_id=789,
        )

        self.assertEqual(context, "（暂无上下文消息）")
        self.assertEqual(
            recent_message_calls,
            [{"group_id": 123, "limit": 50, "before_message_id": 789}],
        )


if __name__ == "__main__":
    unittest.main()

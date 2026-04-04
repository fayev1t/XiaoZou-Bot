from __future__ import annotations

import importlib
import sys
import types
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _install_conversation_test_stubs() -> None:
    class _DummyLogger:
        def info(self, *args: object, **kwargs: object) -> None:
            _ = args, kwargs

        def warning(self, *args: object, **kwargs: object) -> None:
            _ = args, kwargs

        def error(self, *args: object, **kwargs: object) -> None:
            _ = args, kwargs

        def debug(self, *args: object, **kwargs: object) -> None:
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
    setattr(logging_module, "log_ai_input", lambda *args, **kwargs: None)
    setattr(logging_module, "log_ai_output", lambda *args, **kwargs: None)
    sys.modules["qqbot.core.logging"] = logging_module

    llm_module = types.ModuleType("qqbot.core.llm")

    class LLMConfig:
        pass

    async def create_llm(*args: object, **kwargs: object) -> None:
        _ = args, kwargs
        return None

    setattr(llm_module, "LLMConfig", LLMConfig)
    setattr(llm_module, "create_llm", create_llm)
    sys.modules["qqbot.core.llm"] = llm_module

    prompt_module = types.ModuleType("qqbot.services.prompt")

    class PromptManager:
        @property
        def response_prompt(self) -> str:
            return "response-prompt"

    setattr(prompt_module, "PromptManager", PromptManager)
    sys.modules["qqbot.services.prompt"] = prompt_module

    group_member_module = types.ModuleType("qqbot.services.group_member")
    setattr(group_member_module, "GroupMemberService", object)
    sys.modules["qqbot.services.group_member"] = group_member_module

    user_module = types.ModuleType("qqbot.services.user")
    setattr(user_module, "UserService", object)
    sys.modules["qqbot.services.user"] = user_module

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

    langchain_messages_module = types.ModuleType("langchain_core.messages")

    class HumanMessage:
        def __init__(self, content: object) -> None:
            self.content = content

    class SystemMessage:
        def __init__(self, content: object) -> None:
            self.content = content

    setattr(langchain_messages_module, "HumanMessage", HumanMessage)
    setattr(langchain_messages_module, "SystemMessage", SystemMessage)
    sys.modules["langchain_core.messages"] = langchain_messages_module


def _load_conversation_module() -> Any:
    _install_conversation_test_stubs()
    sys.modules.pop("qqbot.services.conversation", None)
    return importlib.import_module("qqbot.services.conversation")


class ConversationMessageBuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = _load_conversation_module()

    def test_build_messages_supports_multimodal_image_inputs(self) -> None:
        service = self.module.ConversationService()
        messages = service._build_messages(
            response_prompt="请根据这些内容回复",
            image_inputs=[
                {
                    "type": "text",
                    "text": "以下图片对应的 file_hash 是 hash-1。",
                },
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,abc"},
                }
            ],
        )

        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0].content, "response-prompt")
        self.assertIsInstance(messages[1].content, list)
        self.assertEqual(messages[1].content[0]["type"], "text")
        self.assertEqual(messages[1].content[0]["text"], "请根据这些内容回复")
        self.assertEqual(messages[1].content[1]["type"], "text")
        self.assertEqual(messages[1].content[2]["type"], "image_url")


if __name__ == "__main__":
    unittest.main()

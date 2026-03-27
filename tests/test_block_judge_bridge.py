from __future__ import annotations

import importlib
import sys
import types
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _install_block_judge_test_stubs() -> None:
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
    setattr(logging_module, "log_ai_input", lambda *args, **kwargs: None)
    setattr(logging_module, "log_ai_output", lambda *args, **kwargs: None)
    sys.modules["qqbot.core.logging"] = logging_module

    llm_module = types.ModuleType("qqbot.core.llm")

    class _DummyLLMConfig:
        pass

    async def _dummy_create_llm(*args: object, **kwargs: object) -> None:
        _ = args, kwargs
        return None

    setattr(llm_module, "LLMConfig", _DummyLLMConfig)
    setattr(llm_module, "create_llm", _dummy_create_llm)
    sys.modules["qqbot.core.llm"] = llm_module

    silence_module = types.ModuleType("qqbot.services.silence_mode")
    setattr(silence_module, "is_silent", lambda group_id: False)
    setattr(silence_module, "set_silent", lambda group_id, value: None)
    sys.modules["qqbot.services.silence_mode"] = silence_module

    prompt_module = types.ModuleType("qqbot.services.prompt")

    class _DummyPromptManager:
        @property
        def block_judge_prompt(self) -> str:
            return "stub prompt"

    setattr(prompt_module, "PromptManager", _DummyPromptManager)
    sys.modules["qqbot.services.prompt"] = prompt_module


def _load_block_judge_module() -> Any:
    _install_block_judge_test_stubs()
    sys.modules.pop("qqbot.services.block_judge", None)
    return importlib.import_module("qqbot.services.block_judge")


block_judge = _load_block_judge_module()


class BlockJudgeParsingTests(unittest.TestCase):
    def test_from_dict_supports_minimal_reply_items(self) -> None:
        payload = {
            "topic_count": 2,
            "replies": [
                {
                    "should_reply": "YES",
                    "instruction": " 先回小林追问的截图问题，再补一句吐槽 ",
                    "target_user_id": "1001",
                    "should_mention": "1",
                    "related_image_hashes": [
                        " hash-1 ",
                        "",
                        None,
                        "hash-2",
                        "hash-1",
                        123,
                    ],
                },
                {
                    "should_reply": "0",
                    "instruction": "这个闲聊话题群友已经接住了",
                    "target_user_id": "",
                    "should_mention": "false",
                    "related_image_hashes": " hash-3 ",
                },
            ],
            "explanation": None,
            "should_enter_silence_mode": "0",
            "should_exit_silence_mode": "true",
        }

        result = block_judge.BlockJudgeResult.from_dict(payload)

        self.assertTrue(result.should_reply)
        self.assertEqual(result.reply_count, 1)
        self.assertEqual(result.topic_count, 2)
        self.assertEqual(result.explanation, "")
        self.assertFalse(result.should_enter_silence_mode)
        self.assertTrue(result.should_exit_silence_mode)

        first_reply = result.replies[0]
        self.assertTrue(first_reply.should_reply)
        self.assertEqual(first_reply.instruction, "先回小林追问的截图问题，再补一句吐槽")
        self.assertEqual(first_reply.target_user_id, 1001)
        self.assertTrue(first_reply.should_mention)
        self.assertEqual(first_reply.related_image_hashes, ["hash-1", "hash-2"])

        second_reply = result.replies[1]
        self.assertFalse(second_reply.should_reply)
        self.assertEqual(second_reply.instruction, "这个闲聊话题群友已经接住了")
        self.assertIsNone(second_reply.target_user_id)
        self.assertFalse(second_reply.should_mention)
        self.assertEqual(second_reply.related_image_hashes, ["hash-3"])

    def test_replies_order_is_preserved_and_reply_count_comes_from_sendable_topics(self) -> None:
        payload = {
            "topic_count": 3,
            "replies": [
                {
                    "should_reply": False,
                    "instruction": "第一个话题不用插话",
                },
                "ignore-me",
                {
                    "should_reply": True,
                    "instruction": "第二个话题先回问题",
                },
                {
                    "should_reply": True,
                    "instruction": "第三个话题补一句接梗",
                },
            ],
        }

        result = block_judge.BlockJudgeResult.from_dict(payload)

        self.assertTrue(result.should_reply)
        self.assertEqual(result.reply_count, 2)
        self.assertEqual(result.topic_count, 3)
        self.assertEqual(
            [reply.should_reply for reply in result.replies],
            [False, True, True],
        )
        self.assertEqual(
            [reply.instruction for reply in result.replies],
            ["第一个话题不用插话", "第二个话题先回问题", "第三个话题补一句接梗"],
        )


class BlockJudgeContextTests(unittest.TestCase):
    def test_build_layer3_context_keeps_minimal_sections(self) -> None:
        context_text = block_judge.build_layer3_context(
            context="历史上下文",
            current_block_text="<System-Message user_id=\"1\">当前对话块</System-Message>",
        )

        self.assertIn("【历史上下文】\n历史上下文", context_text)
        self.assertIn(
            "【当前对话块】\n<System-Message user_id=\"1\">当前对话块</System-Message>",
            context_text,
        )
        self.assertNotIn("【当前对话块摘要】", context_text)
        self.assertNotIn("【本轮回复任务】", context_text)


if __name__ == "__main__":
    unittest.main()

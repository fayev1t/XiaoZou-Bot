from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROMPT_FILE = ROOT / "qqbot" / "services" / "prompt.py"


def _load_prompt_manager_class() -> type:
    spec = importlib.util.spec_from_file_location("test_prompt_module", PROMPT_FILE)
    if spec is None or spec.loader is None:
        raise AssertionError(f"failed to load prompt module from {PROMPT_FILE}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.PromptManager


PromptManager = _load_prompt_manager_class()


class PromptManagerContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = PromptManager()

    def test_shared_system_xml_guide_documents_actual_tags_and_attrs(self) -> None:
        guide = self.manager._system_xml_protocol_prompt
        required_tokens = [
            "System-Message",
            "msg_hash",
            "System-PureText",
            "System-At",
            "System-Reply",
            "System-QQFace",
            "System-Image",
            "System-ToolCall",
            "System-AudioPlaceholder",
            "System-FilePlaceholder",
            "System-Other",
            "System-Unknown",
            "user_id",
            "display_name",
            "timestamp",
            "qq_face_id",
            "file_hash",
            "tool",
            "input",
            "record_size",
            "record_duration",
            "file_size",
            "file_name",
            "file_format",
            "type",
            "unknown_type",
        ]

        for token in required_tokens:
            with self.subTest(token=token):
                self.assertIn(token, guide)

    def test_block_judge_prompt_includes_shared_guide_and_layer2_contract(self) -> None:
        guide = self.manager._system_xml_protocol_prompt
        prompt = self.manager.block_judge_prompt

        self.assertIn(guide, prompt)
        self.assertIn("你是 Layer 2 结构化回复规划器", prompt)
        self.assertIn("【你的工作顺序】", prompt)
        self.assertIn("顶层输出始终只能是一个 JSON 对象", prompt)
        self.assertIn('"topic_count": number', prompt)
        self.assertIn('"should_reply": bool', prompt)
        self.assertIn('"instruction": string', prompt)
        self.assertIn('"tool_calls": [', prompt)
        self.assertIn('"tool": string', prompt)
        self.assertIn('"msg_hash": string', prompt)
        self.assertIn("image_parse", prompt)
        self.assertIn("file_hash", prompt)
        self.assertIn('"replies": [', prompt)
        self.assertIn('"should_exit_silence_mode": bool', prompt)
        self.assertIn("当前消息块、显式 @、`System-Reply` 直接线程，优先于更早历史", prompt)
        self.assertIn("不要替 Layer 3 写具体措辞", prompt)
        self.assertNotIn('"reply_count": number', prompt)
        self.assertNotIn('"block_summary": string', prompt)
        self.assertNotIn('"emotion": string', prompt)
        self.assertNotIn('"related_image_hashes": [string]', prompt)
        self.assertNotIn("target_display_name", prompt)
        self.assertNotIn("plan_id", prompt)
        self.assertNotIn("thread_summary", prompt)
        self.assertNotIn("reply_kind", prompt)
        self.assertNotIn("addressing_scope", prompt)
        self.assertNotIn("relation_summary", prompt)
        self.assertNotIn("source_messages", prompt)

    def test_response_prompt_includes_shared_guide_and_layer3_contract(self) -> None:
        guide = self.manager._system_xml_protocol_prompt
        prompt = self.manager.response_prompt

        self.assertTrue(prompt.startswith(self.manager.character_prompt))
        self.assertIn(guide, prompt)
        self.assertNotIn("请参考 Layer 2 提供的 XML 格式说明", prompt)
        self.assertIn("你现在不是一个 AI，你是一个活生生的人。", prompt)
        self.assertIn("输出的时候 **绝对不要** 带任何 XML 标签", prompt)
        self.assertIn("当前对话块", prompt)
        self.assertIn("System-ToolCall", prompt)
        self.assertIn("图片本身一并作为多模态输入", prompt)
        self.assertIn("instruction", prompt)
        self.assertIn("当前对话块 / 当前直接线程 > 显式 @ 与 Reply 关系 > 更早历史", prompt)
        self.assertIn("不要把它误当聊天记录中的自然发言", prompt)
        self.assertIn("不要忽略图片本体", prompt)
        self.assertNotIn("【情绪】（emotion）", prompt)
        self.assertIn(
            "根据指导和相关工具结果，生成一条符合小奏人设、像真人一样的群聊回复。",
            prompt,
        )

    def test_wait_time_judge_prompt_includes_shared_guide_and_layer1_contract(self) -> None:
        prompt = self.manager.wait_time_judge_prompt

        self.assertNotIn(self.manager._system_xml_protocol_prompt, prompt)
        self.assertIn("你是群聊消息聚合专家", prompt)
        self.assertIn("当前消息块", prompt)
        self.assertIn("原始文本拼接结果", prompt)
        self.assertIn("只根据当前消息块本身判断", prompt)
        self.assertIn('"should_wait": true/false', prompt)
        self.assertIn(
            '"wait_seconds": 数字(3-10秒，仅当should_wait=true时填写)',
            prompt,
        )
        self.assertIn('"reason": "简短说明判断依据"', prompt)

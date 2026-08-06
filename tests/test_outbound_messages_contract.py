"""outbound_messages 共享校验合同（2026-07-31 自 replyer/send_message 迁出）。

钉住三层：
- ``normalize_segment``：LLM 输出的已知漂移（段字段拍平、reply 的
  message_id 别名）无损归一，其余形态原样透传交严格校验 fail loudly；
- ``validate_content``：OneBot 段白名单 + 结构 + 顺序 + 每段字段；
- ``validate_messages``：1–10 气泡、meme 不限量、chat/meme 形状。
"""

from __future__ import annotations

import unittest

from qqbot.services.agent_loop.outbound_messages import (
    MAX_OUTBOUND_MESSAGES,
    delivery_status,
    extract_message_id,
    normalize_segment,
    public_receipt,
    validate_content,
    validate_messages,
)

HASH_A = "ab" * 32
_TEXT = {"type": "text", "data": {"text": "好"}}


class NormalizeSegmentTests(unittest.TestCase):
    def test_flat_segments_are_wrapped_into_data(self) -> None:
        """Gemini 系模型的真实漂移形态（2026-07-22 线上快照）：段字段拍平到
        顶层。归一成 OneBot data 包装后，严格校验保持不变。"""
        self.assertEqual(
            normalize_segment({"type": "text", "text": "在呢"}),
            {"type": "text", "data": {"text": "在呢"}},
        )
        self.assertEqual(
            normalize_segment({"type": "at", "qq": "10001"}),
            {"type": "at", "data": {"qq": "10001"}},
        )
        self.assertEqual(
            normalize_segment({"type": "reply", "id": "1115629605"}),
            {"type": "reply", "data": {"id": "1115629605"}},
        )

    def test_reply_message_id_alias_is_normalized(self) -> None:
        self.assertEqual(
            normalize_segment(
                {"type": "reply", "data": {"message_id": "840063058"}}
            ),
            {"type": "reply", "data": {"id": "840063058"}},
        )

    def test_unrecognized_shapes_pass_through_untouched(self) -> None:
        """归一只处理已知漂移；其余坏形态原样透传，由严格校验 fail loudly，
        不在归一层静默吞掉。"""
        for segment in (
            {"type": "image", "data": {"file": "x"}},
            {"type": "text", "data": "hello"},
            "not-a-dict",
        ):
            with self.subTest(segment=segment):
                self.assertEqual(normalize_segment(segment), segment)


class ValidateContentTests(unittest.TestCase):
    def test_valid_combo_passes(self) -> None:
        self.assertIsNone(
            validate_content(
                [
                    {"type": "reply", "data": {"id": "M1"}},
                    {"type": "at", "data": {"qq": "99999"}},
                    {"type": "text", "data": {"text": " hi"}},
                    {"type": "face", "data": {"id": "178"}},
                ]
            )
        )

    def test_empty_and_blank_content_fail(self) -> None:
        fail = validate_content([])
        assert fail is not None
        self.assertEqual(fail.extra["reason_code"], "content_empty")
        fail = validate_content([{"type": "text", "data": {"text": "   "}}])
        assert fail is not None
        self.assertEqual(fail.extra["reason_code"], "content_all_blank")

    def test_unsupported_segment_type_is_named_precisely(self) -> None:
        fail = validate_content(
            [_TEXT, {"type": "image", "data": {"file": "x.png"}}]
        )
        assert fail is not None
        self.assertEqual(fail.error_kind, "invalid_arguments")
        self.assertEqual(fail.extra["reason_code"], "unsupported_segment_type")
        self.assertEqual(fail.extra["segment_index"], 1)
        self.assertEqual(fail.extra["segment_type"], "image")

    def test_reply_segment_rules(self) -> None:
        fail = validate_content(
            [_TEXT, {"type": "reply", "data": {"id": "M1"}}]
        )
        assert fail is not None
        self.assertEqual(fail.extra["reason_code"], "reply_segment_not_first")
        fail = validate_content(
            [
                {"type": "reply", "data": {"id": "M1"}},
                {"type": "reply", "data": {"id": "M2"}},
            ]
        )
        assert fail is not None
        self.assertEqual(fail.extra["reason_code"], "duplicate_reply_segment")

    def test_at_all_is_legal(self) -> None:
        self.assertIsNone(
            validate_content(
                [{"type": "at", "data": {"qq": "all"}}, _TEXT]
            )
        )


class ValidateMessagesTests(unittest.TestCase):
    def test_ordered_chat_and_one_meme_pass_and_normalize(self) -> None:
        normalized, fail = validate_messages(
            [
                {"kind": "chat", "content": [{"type": "text", "text": "先说"}]},
                {"kind": "meme", "image_hash": HASH_A},
                {"kind": "chat", "content": [_TEXT]},
            ]
        )
        self.assertIsNone(fail)
        self.assertEqual(
            [item["kind"] for item in normalized], ["chat", "meme", "chat"]
        )
        # 拍平段已被归一
        self.assertEqual(
            normalized[0]["content"],
            [{"type": "text", "data": {"text": "先说"}}],
        )

    def test_empty_messages_is_invalid(self) -> None:
        for messages in ([], None, "hi"):
            with self.subTest(messages=messages):
                _, fail = validate_messages(messages)
                assert fail is not None
                self.assertEqual(fail.error_kind, "invalid_arguments")

    def test_bubble_count_is_capped(self) -> None:
        """上限 10（2026-07-31 自 4 放宽），到顶通过、超一条即拒。"""
        self.assertEqual(MAX_OUTBOUND_MESSAGES, 10)
        normalized, fail = validate_messages(
            [{"kind": "chat", "content": [_TEXT]}] * MAX_OUTBOUND_MESSAGES
        )
        self.assertIsNone(fail)
        self.assertEqual(len(normalized), MAX_OUTBOUND_MESSAGES)
        _, fail = validate_messages(
            [{"kind": "chat", "content": [_TEXT]}]
            * (MAX_OUTBOUND_MESSAGES + 1)
        )
        assert fail is not None
        self.assertEqual(fail.extra["reason_code"], "too_many_messages")

    def test_multiple_memes_are_allowed(self) -> None:
        """meme 单次限量已取消（2026-07-31）：只要不超总条数就放行。"""
        normalized, fail = validate_messages(
            [{"kind": "meme", "image_hash": HASH_A}] * 3
        )
        self.assertIsNone(fail)
        self.assertEqual([item["kind"] for item in normalized], ["meme"] * 3)

    def test_bad_meme_hash_is_rejected(self) -> None:
        for image_hash in ("short", 123, None, "z" * 64):
            with self.subTest(image_hash=image_hash):
                _, fail = validate_messages(
                    [{"kind": "meme", "image_hash": image_hash}]
                )
                assert fail is not None
                self.assertEqual(fail.extra["reason_code"], "bad_image_hash")

    def test_unknown_kind_and_unknown_keys_fail_loudly(self) -> None:
        _, fail = validate_messages([{"kind": "verbatim", "content": []}])
        assert fail is not None
        self.assertEqual(fail.extra["reason_code"], "bad_message_kind")
        _, fail = validate_messages(
            [{"kind": "chat", "content": [_TEXT], "tone": "soft"}]
        )
        assert fail is not None
        self.assertEqual(fail.extra["reason_code"], "unexpected_argument")


class ReceiptHelperTests(unittest.TestCase):
    def test_delivery_status_keeps_unknown_delivery_distinct(self) -> None:
        self.assertEqual(delivery_status([{"status": "sent"}]), "sent")
        self.assertEqual(
            delivery_status([{"status": "sent"}, {"status": "failed"}]),
            "partial",
        )
        self.assertEqual(
            delivery_status([{"status": "sent"}, {"status": "uncertain"}]),
            "uncertain",
        )
        self.assertEqual(delivery_status([{"status": "uncertain"}]), "uncertain")
        self.assertEqual(delivery_status([{"status": "failed"}]), "failed")

    def test_public_receipt_redacts_base64_and_binary_values(self) -> None:
        receipt = public_receipt(
            {
                "status": "ok",
                "echo": {
                    "message": [
                        {
                            "file": "base64://secret-payload",
                            "raw": b"secret-bytes",
                        }
                    ]
                },
            }
        )
        self.assertEqual(receipt["status"], "ok")
        self.assertEqual(
            receipt["echo"]["message"][0]["file"], "<base64-redacted>"
        )
        self.assertEqual(
            receipt["echo"]["message"][0]["raw"], "<binary-redacted>"
        )

    def test_extract_message_id_accepts_dict_and_int(self) -> None:
        self.assertEqual(extract_message_id({"message_id": 42}), 42)
        self.assertEqual(extract_message_id(42), 42)
        self.assertIsNone(extract_message_id({"status": "ok"}))


if __name__ == "__main__":
    unittest.main()

"""outbound_messages 共享校验合同（2026-07-31 自 replyer/send_message 迁出）。

钉住三层：
- ``normalize_segment``：LLM 输出的已知漂移（段字段拍平、reply 的
  message_id 别名）无损归一，其余形态原样透传交严格校验 fail loudly；
- ``validate_content``：OneBot 段白名单 + 结构 + 顺序 + 每段字段；
- ``validate_messages``：1–10 气泡、meme 不限量、领域气泡形状；
- ``build_chat_content``：领域气泡 → OneBot 段，出站协议知识的唯一落点。

2026-08-14 去协议化后，前两个函数只在旧形状兼容路径上还有消费者
（``_legacy_bubble_to_domain``），但它们的规则仍是现役的——``build_chat_content``
构造出的段必须继续满足 ``validate_content``。
"""

from __future__ import annotations

import unittest

from qqbot.services.agent_loop.outbound_messages import (
    MAX_OUTBOUND_MESSAGES,
    build_chat_content,
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
    """2026-08-14 起 messages 是领域形状：一项就是一条消息，键名即语义。"""

    def test_ordered_text_and_meme_pass_and_normalize(self) -> None:
        normalized, fail = validate_messages(
            [
                {"text": "先说"},
                {"meme": HASH_A.upper()},
                {"text": "你说得对", "at": 10001},
            ]
        )
        self.assertIsNone(fail)
        self.assertEqual(
            [item["kind"] for item in normalized], ["chat", "meme", "chat"]
        )
        self.assertEqual(normalized[0], {"kind": "chat", "text": "先说"})
        # hash 归一为小写；单值 at 归一成列表，调用方不必分情况。
        self.assertEqual(normalized[1]["image_hash"], HASH_A)
        self.assertEqual(normalized[2]["at"], ["10001"])

    def test_optional_keys_are_omitted_when_absent(self) -> None:
        """缺省的键不进归一结果——回执与投影读到的就是模型真正写了什么。"""
        normalized, _ = validate_messages([{"text": "hi"}])
        self.assertEqual(sorted(normalized[0]), ["kind", "text"])

    def test_text_may_be_omitted_when_at_or_face_carries_the_bubble(self) -> None:
        for bubble in ({"at": 10001}, {"face": 178}):
            with self.subTest(bubble=bubble):
                _, fail = validate_messages([bubble])
                self.assertIsNone(fail)

    def test_bubble_without_any_visible_payload_is_rejected(self) -> None:
        for bubble in ({"text": "   "}, {"text": ""}, {}):
            with self.subTest(bubble=bubble):
                _, fail = validate_messages([bubble])
                assert fail is not None
                self.assertEqual(fail.extra["reason_code"], "bubble_all_blank")

    def test_at_accepts_single_list_and_all(self) -> None:
        normalized, fail = validate_messages(
            [{"text": "在", "at": [10001, "all"]}]
        )
        self.assertIsNone(fail)
        self.assertEqual(normalized[0]["at"], ["10001", "all"])
        for bad in (0, -1, "abc", True, {}):
            with self.subTest(bad=bad):
                _, fail = validate_messages([{"text": "在", "at": bad}])
                assert fail is not None
                self.assertEqual(fail.extra["reason_code"], "bad_at_target")

    def test_face_rejects_all_and_non_numeric(self) -> None:
        """face 是系统表情 ID，没有 "all" 这种目标语义。"""
        for bad in ("all", "x", -1):
            with self.subTest(bad=bad):
                _, fail = validate_messages([{"text": "在", "face": bad}])
                assert fail is not None
                self.assertEqual(fail.extra["reason_code"], "bad_face_id")

    def test_reply_accepts_string_or_int_and_rejects_empty(self) -> None:
        for value in ("1115629605", 1115629605):
            with self.subTest(value=value):
                normalized, fail = validate_messages(
                    [{"text": "hi", "reply": value}]
                )
                self.assertIsNone(fail)
                self.assertEqual(normalized[0]["reply"], "1115629605")
        for bad in ("", "   ", None, []):
            with self.subTest(bad=bad):
                _, fail = validate_messages([{"text": "hi", "reply": bad}])
                assert fail is not None
                self.assertEqual(fail.extra["reason_code"], "bad_reply_target")

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
            [{"text": "hi"}] * MAX_OUTBOUND_MESSAGES
        )
        self.assertIsNone(fail)
        self.assertEqual(len(normalized), MAX_OUTBOUND_MESSAGES)
        _, fail = validate_messages([{"text": "hi"}] * (MAX_OUTBOUND_MESSAGES + 1))
        assert fail is not None
        self.assertEqual(fail.extra["reason_code"], "too_many_messages")

    def test_multiple_memes_are_allowed(self) -> None:
        """meme 单次限量已取消（2026-07-31）：只要不超总条数就放行。"""
        normalized, fail = validate_messages([{"meme": HASH_A}] * 3)
        self.assertIsNone(fail)
        self.assertEqual([item["kind"] for item in normalized], ["meme"] * 3)

    def test_bad_meme_hash_is_rejected(self) -> None:
        for image_hash in ("short", 123, None, "z" * 64):
            with self.subTest(image_hash=image_hash):
                _, fail = validate_messages([{"meme": image_hash}])
                assert fail is not None
                self.assertEqual(fail.extra["reason_code"], "bad_image_hash")

    def test_meme_bubble_cannot_carry_text(self) -> None:
        """表情包气泡只发图；要配文字就另起一条——不重新造出"图文一体"的段。"""
        _, fail = validate_messages([{"meme": HASH_A, "text": "看这个"}])
        assert fail is not None
        self.assertEqual(fail.extra["reason_code"], "unexpected_argument")

    def test_unknown_keys_fail_loudly(self) -> None:
        _, fail = validate_messages([{"text": "hi", "tone": "soft"}])
        assert fail is not None
        self.assertEqual(fail.extra["reason_code"], "unexpected_argument")
        _, fail = validate_messages([{"image": "x.png"}])
        assert fail is not None
        self.assertEqual(fail.extra["reason_code"], "unexpected_argument")


class BuildChatContentTests(unittest.TestCase):
    """出站协议知识的唯一落点（2026-08-14）。

    模型不再写消息段，所以段序不再是它能控制的东西——reply 必须 content[0]
    这条 OneBot 规则由这里保证，而不是由提示词纪律保证。
    """

    def test_segment_order_is_reply_at_text_face(self) -> None:
        normalized, _ = validate_messages(
            [{"text": "hi", "reply": "M1", "at": [10001, "all"], "face": 178}]
        )
        self.assertEqual(
            build_chat_content(normalized[0]),
            [
                {"type": "reply", "data": {"id": "M1"}},
                {"type": "at", "data": {"qq": "10001"}},
                {"type": "at", "data": {"qq": "all"}},
                {"type": "text", "data": {"text": "hi"}},
                {"type": "face", "data": {"id": "178"}},
            ],
        )

    def test_absent_keys_emit_no_segments(self) -> None:
        normalized, _ = validate_messages([{"text": "hi"}])
        self.assertEqual(
            build_chat_content(normalized[0]),
            [{"type": "text", "data": {"text": "hi"}}],
        )
        normalized, _ = validate_messages([{"at": 10001}])
        self.assertEqual(
            build_chat_content(normalized[0]),
            [{"type": "at", "data": {"qq": "10001"}}],
        )

    def test_built_segments_pass_the_legacy_validator(self) -> None:
        """构造出来的段必须仍满足 validate_content 的全部规则——段白名单、
        reply 至多一个且在 content[0]、可见负载非空。"""
        normalized, _ = validate_messages(
            [{"text": "hi", "reply": "M1", "at": 10001, "face": 178}]
        )
        self.assertIsNone(validate_content(build_chat_content(normalized[0])))


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

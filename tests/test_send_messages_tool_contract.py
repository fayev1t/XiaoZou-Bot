"""SendMessagesTool 合同（2026-07-31 删除 Replyer：Planner 亲自发言的出口）。

钉住四组边界（重构提案-删除Replyer.md §4、§8）：
- 普通 ToolWorker 工具：不读 ReplyTask、不查完成事件——没有任何完成事件时
  调用照常执行；不写任何领域/runtime 事件（发送事实只活在 terminal 里）；
- 静态校验与 meme preflight 失败无副作用（不碰 OneBot）；
- 结果语义：sent → success；partial / failed / uncertain → failure，
  status 与完整逐条 receipts 经 extra 平铺进 tool_failed payload，供投影
  派生 `<my-reply>`；
- allowed_scopes 只有 group；回执脱敏（base64 不进事件流）。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from qqbot.services.agent_loop import bot_registry
from qqbot.services.agent_loop.outbound_messages import (
    _ALLOWED_SEGMENT_TYPES,
    MAX_OUTBOUND_MESSAGES,
    validate_messages,
)
from qqbot.services.agent_loop.tools.send_messages import SendMessagesTool

HASH_A = "ab" * 32
_TEXT = {"type": "text", "data": {"text": "hi"}}


class _FakeActionFailed(Exception):
    def __init__(self, retcode: int, wording: str) -> None:
        super().__init__(f"ActionFailed: retcode={retcode}")
        self.info = {
            "status": "failed",
            "retcode": retcode,
            "message": "",
            "wording": wording,
            "stream": "normal-action",
        }


class _StubBot:
    def __init__(
        self,
        message_ids: list[int | None] | None = None,
        raises: list[Exception | None] | None = None,
    ) -> None:
        self.self_id = "10001"
        self._message_ids = message_ids or [111, 222, 333, 444]
        self._raises = raises or []
        self.calls: list[dict] = []

    async def send_group_msg(self, **kwargs: Any) -> dict:
        index = len(self.calls)
        self.calls.append(kwargs)
        if index < len(self._raises) and self._raises[index] is not None:
            raise self._raises[index]  # type: ignore[misc]
        mid = self._message_ids[index] if index < len(self._message_ids) else 999
        return {"message_id": mid} if mid is not None else {}


def _context(**overrides: Any) -> dict:
    ctx: dict[str, Any] = {
        "scope_key": "group:100",
        "session_factory": object(),
        "correlation_id": "CID",
        "tool_call_event_id": "E_TOOL_CALL",
    }
    ctx.update(overrides)
    return ctx


class SendMessagesToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        bot_registry.clear()

    def tearDown(self) -> None:
        bot_registry.clear()

    async def _run(self, arguments: dict, *, context: dict | None = None):
        return await SendMessagesTool().run(
            arguments, **(context or _context())
        )

    # ── 主路径：普通工具，不读 ReplyTask ──

    async def test_send_without_any_completed_event_still_executes(self) -> None:
        """§0.5 软约束：运行时不因缺少完成事件拒绝调用；工具也不查询
        ReplyTask——这里没有打任何 reply_task 相关补丁，调用照常成功。"""
        bot = _StubBot()
        bot_registry.register(bot)
        outcome = await self._run(
            {
                "messages": [
                    {"kind": "chat", "content": [_TEXT]},
                    {"kind": "chat", "content": [_TEXT]},
                ]
            }
        )
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.result["status"], "sent")
        self.assertEqual(outcome.result["message_ids"], [111, 222])
        self.assertEqual(len(bot.calls), 2)
        self.assertEqual(bot.calls[0]["group_id"], 100)
        # receipts 随 result 落 terminal，供投影派生 <my-reply>。
        receipts = outcome.result["sent_messages"]
        self.assertEqual(
            [item["status"] for item in receipts], ["sent", "sent"]
        )
        self.assertEqual(receipts[0]["self_id"], "10001")

    async def test_meme_bubble_is_loaded_and_sent_as_image(self) -> None:
        bot = _StubBot()
        bot_registry.register(bot)
        fake_meme = SimpleNamespace(file_hash=HASH_A)
        with (
            patch(
                "qqbot.services.agent_loop.meme_store.get_meme",
                new=AsyncMock(return_value=fake_meme),
            ),
            patch(
                "qqbot.services.agent_loop.tools._meme_common."
                "media_path_for_hash",
                return_value=SimpleNamespace(read_bytes=lambda: b"imgbytes"),
            ),
        ):
            outcome = await self._run(
                {"messages": [{"kind": "meme", "image_hash": HASH_A}]}
            )
        self.assertTrue(outcome.ok)
        sent = bot.calls[0]["message"]
        self.assertEqual(sent[0]["type"], "image")
        self.assertTrue(sent[0]["data"]["file"].startswith("base64://"))
        # 落进 terminal 的回执必须脱敏，不携带 base64 正文。
        self.assertNotIn("base64://", str(outcome.result))
        self.assertEqual(
            outcome.result["sent_messages"][0]["image_hash"], HASH_A
        )

    async def test_two_distinct_commands_both_execute(self) -> None:
        """两条不同的发送命令都会执行——运行时不合并、不去重；约束模型的
        只有提示词。"""
        bot = _StubBot()
        bot_registry.register(bot)
        for event_id in ("E_CALL_A", "E_CALL_B"):
            outcome = await self._run(
                {"messages": [{"kind": "chat", "content": [_TEXT]}]},
                context=_context(tool_call_event_id=event_id),
            )
            self.assertTrue(outcome.ok)
        self.assertEqual(len(bot.calls), 2)

    # ── 无副作用的失败：静态校验与 preflight ──

    async def test_static_invalid_never_touches_onebot(self) -> None:
        bot = _StubBot()
        bot_registry.register(bot)
        cases = [
            {"messages": []},
            {"messages": [{"kind": "verbatim"}]},
            {"messages": [{"kind": "chat", "content": [_TEXT]}], "tone": "x"},
            {"messages": [{"kind": "meme", "image_hash": "short"}]},
        ]
        for arguments in cases:
            with self.subTest(arguments=arguments):
                outcome = await self._run(arguments)
                self.assertFalse(outcome.ok)
                self.assertEqual(outcome.error_kind, "invalid_arguments")
        self.assertEqual(bot.calls, [])

    async def test_meme_gone_fails_before_sending(self) -> None:
        bot = _StubBot()
        bot_registry.register(bot)
        with patch(
            "qqbot.services.agent_loop.meme_store.get_meme",
            new=AsyncMock(return_value=None),
        ):
            outcome = await self._run(
                {
                    "messages": [
                        {"kind": "chat", "content": [_TEXT]},
                        {"kind": "meme", "image_hash": HASH_A},
                    ]
                }
            )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "invalid_arguments")
        self.assertEqual(outcome.extra["reason_code"], "meme_not_saved")
        self.assertEqual(bot.calls, [])

    async def test_no_bot_available(self) -> None:
        outcome = await self._run(
            {"messages": [{"kind": "chat", "content": [_TEXT]}]}
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "no_bot_available")

    # ── 结果语义：status 与终态配对，receipts 平铺进 payload ──

    async def test_partial_is_failure_with_full_receipts(self) -> None:
        """§4.3：部分成功 → failure("upstream_action_failed") + status=
        "partial" + 完整逐条 receipts——投影据此把已 sent 气泡渲染为既成
        事实。"""
        bot = _StubBot(raises=[None, _FakeActionFailed(1404, "群不存在")])
        bot_registry.register(bot)
        outcome = await self._run(
            {
                "messages": [
                    {"kind": "chat", "content": [_TEXT]},
                    {"kind": "chat", "content": [_TEXT]},
                ]
            }
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "upstream_action_failed")
        self.assertEqual(outcome.extra["status"], "partial")
        self.assertEqual(outcome.extra["message_ids"], [111])
        receipts = outcome.extra["sent_messages"]
        self.assertEqual(
            [item["status"] for item in receipts], ["sent", "failed"]
        )

    async def test_all_failed_is_failure_with_status_failed(self) -> None:
        bot = _StubBot(raises=[_FakeActionFailed(1404, "群不存在")])
        bot_registry.register(bot)
        outcome = await self._run(
            {"messages": [{"kind": "chat", "content": [_TEXT]}]}
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "upstream_action_failed")
        self.assertEqual(outcome.extra["status"], "failed")
        self.assertIn("群不存在", outcome.error_message)

    async def test_transport_exception_is_uncertain(self) -> None:
        """OneBot 调用中断（非 ActionFailed 的裸异常）：该气泡可能已发出，
        整体收敛 uncertain——终态失败，提示词禁止"保险再发一遍"。"""
        bot = _StubBot(raises=[RuntimeError("socket closed")])
        bot_registry.register(bot)
        outcome = await self._run(
            {"messages": [{"kind": "chat", "content": [_TEXT]}]}
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.extra["status"], "uncertain")
        self.assertEqual(
            outcome.extra["sent_messages"][0]["status"], "uncertain"
        )

    async def test_missing_message_id_is_uncertain_not_success(self) -> None:
        bot = _StubBot(message_ids=[None])
        bot_registry.register(bot)
        outcome = await self._run(
            {"messages": [{"kind": "chat", "content": [_TEXT]}]}
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.extra["status"], "uncertain")

    # ── scope 边界 ──

    async def test_group_only_scope(self) -> None:
        """私聊没有 AgentLoop、system 没有聊天目标：allowed_scopes 只有
        group，不照抄旧 send_message 的 ("group", "private")。"""
        self.assertEqual(SendMessagesTool.allowed_scopes, ("group",))
        bot_registry.register(_StubBot())
        for scope_key in ("system", "private:555"):
            with self.subTest(scope_key=scope_key):
                outcome = await self._run(
                    {"messages": [{"kind": "chat", "content": [_TEXT]}]},
                    context=_context(scope_key=scope_key),
                )
                self.assertFalse(outcome.ok)
                self.assertEqual(
                    outcome.error_kind, "tool_unavailable_in_scope"
                )


class SendMessagesMetadataTests(unittest.TestCase):
    def test_name_and_schema(self) -> None:
        self.assertEqual(SendMessagesTool.name, "send_messages")
        schema = SendMessagesTool.arguments_schema
        self.assertEqual(schema["required"], ["messages"])
        self.assertFalse(schema["additionalProperties"])
        # 只有 messages 一个业务参数：没有 target / token / 完成事件 ID。
        self.assertEqual(sorted(schema["properties"]), ["messages"])

    def test_usage_doc_records_uncertain_and_partial_semantics(self) -> None:
        """§5.3：uncertain 表示可能已送达，新调用可能造成重复；partial 按
        逐条 receipt 表示既成发送事实；调用行自己的回执就是发言记录（不派生
        <my-reply>）。完成事件不是发送参数，文档不使用 token 式措辞。"""
        doc = SendMessagesTool.usage_prompt
        self.assertIn("可能已经送达", doc)
        self.assertIn("再次调用会产生新的独立发送命令", doc)
        self.assertIn("逐气泡回执", doc)
        self.assertIn("不会另外生成", doc)
        for forbidden in ("授权", "兑换", "消费", "领取"):
            self.assertNotIn(forbidden, doc)

    def test_usage_doc_does_not_advertise_the_missing_gate(self) -> None:
        """2026-08-01：运行时确实不检查完成事件（§0.5 有意的软约束，不得补
        授权门闩），但**用法文档不再主动向模型交底这一点**。

        "正常在完成事件之后发言"是提示词纪律；一份工具用法文档没有义务告诉
        模型某条纪律缺少强制力——那句话唯一的作用就是邀请它绕过流程。实现
        事实仍写在 send_messages.py 的 docstring 里，供维护者查阅。
        """
        doc = SendMessagesTool.usage_prompt
        for leak in ("运行时允许独立调用", "不会检查", "不校验"):
            self.assertNotIn(leak, doc)

    def test_description_names_the_two_step_flow(self) -> None:
        desc = SendMessagesTool.description
        self.assertIn("<reply-task-completed>", desc)
        self.assertIn("逐气泡回执", desc)
        self.assertIn("可能已经送达", desc)

    def test_bubble_cap_lives_in_schema_and_usage_doc_only(self) -> None:
        """条数上限只有一处真相（outbound_messages），schema 直接引用它；
        具体数字只在工具介绍（usage doc）里写明，description 与其它提示词
        层一律只说"多条"（2026-07-31 放宽到 10、meme 不再限量）。"""
        schema = SendMessagesTool.arguments_schema
        self.assertEqual(
            schema["properties"]["messages"]["maxItems"], MAX_OUTBOUND_MESSAGES
        )
        self.assertIn(
            f"1–{MAX_OUTBOUND_MESSAGES} 个有序气泡",
            SendMessagesTool.usage_prompt,
        )
        self.assertNotIn("1-4", SendMessagesTool.description)
        self.assertNotIn("at most one", SendMessagesTool.description)

    def test_bubble_schema_presents_chat_and_meme_as_peer_branches(self) -> None:
        """2026-08-01：`messages.items` 曾是 `{"type": "object"}`——两种气泡
        唯一同框的地方是 description 那段散文，chat 占前 2/3、meme 挂在分号
        后面当尾巴。模型逐 token 写 JSON 时最强的结构先验是 schema，那等于
        表情包在结构上不存在，而提示词层（planner.md 的"优先让图说话"）却把
        它当惯用表达。这里钉住两支平级：同一个 oneOf 下的兄弟、由 kind 判别、
        各自带完整的 required 与 additionalProperties，没有主次没有嵌套。
        """
        items = SendMessagesTool.arguments_schema["properties"]["messages"][
            "items"
        ]
        branches = items["oneOf"]
        self.assertEqual(
            [b["properties"]["kind"]["const"] for b in branches],
            ["chat", "meme"],
        )
        for branch in branches:
            with self.subTest(kind=branch["properties"]["kind"]["const"]):
                self.assertEqual(branch["required"][0], "kind")
                self.assertFalse(branch["additionalProperties"])
        # 段类型白名单只有一处真相（outbound_messages），schema 不得自成一套。
        segment_type = branches[0]["properties"]["content"]["items"][
            "properties"
        ]["type"]
        self.assertEqual(set(segment_type["enum"]), set(_ALLOWED_SEGMENT_TYPES))

    def test_bubble_schema_shape_matches_validate_messages(self) -> None:
        """schema 是纯文档（tool_registry 模块头），真正的校验是
        validate_messages——两边形状必须逐字对齐，否则模型照 schema 写出的
        气泡会被校验拒绝，而它看不到 schema 之外的真相。

        按每支的 required 造最小气泡送进真校验（必须通过），再多塞一个键
        （必须被拒）——证明 additionalProperties:False 确有 extras 检查兜底，
        不是一句装饰性的文档。
        """
        branches = SendMessagesTool.arguments_schema["properties"]["messages"][
            "items"
        ]["oneOf"]
        minimal = {
            "chat": {"kind": "chat", "content": [_TEXT]},
            "meme": {"kind": "meme", "image_hash": HASH_A},
        }
        for branch in branches:
            kind = branch["properties"]["kind"]["const"]
            with self.subTest(kind=kind):
                bubble = minimal[kind]
                self.assertEqual(sorted(branch["required"]), sorted(bubble))
                normalized, fail = validate_messages([bubble])
                self.assertIsNone(fail)
                self.assertEqual(normalized[0]["kind"], kind)
                _, rejected = validate_messages([{**bubble, "extra": 1}])
                self.assertEqual(
                    getattr(rejected, "error_kind", None), "invalid_arguments"
                )


if __name__ == "__main__":
    unittest.main()

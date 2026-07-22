"""ReplyTask / Replyer 的核心合同测试。

钉住四个边界：reply 取代 send_message；草稿折叠与最终事实分离；合稿去重；
Replyer 一次输出可含多条文本和至多一张已收藏 meme。
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from qqbot.services.agent_loop.reply_task import (
    ReplyTaskState,
    _fold_rows,
    merge_gist,
    merge_targets,
)
from qqbot.services.agent_loop.replyer import (
    _build_system_prompt,
    _parse_output,
)
from qqbot.services.agent_loop.tools import build_default_registry
from qqbot.services.agent_loop.tools.reply import ReplyTool

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 7, 19, 12, 0, tzinfo=TZ)
HASH_A = "ab" * 32


def _event(
    event_id: str,
    event_type: str,
    payload: dict,
    *,
    causation_id: str | None = None,
    seconds: int = 0,
) -> SimpleNamespace:
    return SimpleNamespace(
        event_id=event_id,
        type=event_type,
        payload=payload,
        occurred_at=NOW + timedelta(seconds=seconds),
        scope="group",
        group_id=100,
        user_id=None,
        correlation_id="CID",
        causation_id=causation_id,
    )


def _upsert_payload(revision: int = 1) -> dict:
    return {
        "reply_task_id": "R1",
        "revision": revision,
        "state": "open",
        "created_at": NOW.isoformat(),
        "updated_at": NOW.isoformat(),
        "flush_at": (NOW + timedelta(seconds=10)).isoformat(),
        "hard_deadline": (NOW + timedelta(seconds=90)).isoformat(),
        "mode": "compose",
        "targets": [{"message_id": "M1", "points": ["回答问题"]}],
        "gist": {"intent": "解释清楚", "facts": ["事实 A"]},
        "verbatim_messages": [],
    }


class RegistryBoundaryTests(unittest.TestCase):
    def test_reply_replaces_send_message_and_meme_send_is_removed(self) -> None:
        registry = build_default_registry()
        self.assertIn("reply", registry.names())
        self.assertNotIn("send_message", registry.names())
        meme_schema = registry.get("meme").arguments_schema  # type: ignore[union-attr]
        self.assertEqual(
            meme_schema["properties"]["action"]["enum"],
            ["save", "delete", "recaption"],
        )


class ReplyToolPersistenceTests(unittest.IsolatedAsyncioTestCase):
    def _context(self, notify: AsyncMock) -> dict:
        return {
            "scope_key": "group:100",
            "session_factory": object(),
            "correlation_id": "CID",
            "tool_call_event_id": "E_TOOL_CALL",
            "notify_reply_task": notify,
        }

    async def test_create_returns_pending_identity_not_message_id(self) -> None:
        notify = AsyncMock()
        with (
            patch(
                "qqbot.services.agent_loop.tools.reply.find_upsert_for_tool_call",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "qqbot.services.agent_loop.tools.reply.load_open_reply_task",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "qqbot.services.agent_loop.tools.reply.append_upsert",
                new=AsyncMock(return_value="E_UPSERT"),
            ) as append,
            patch(
                "qqbot.services.agent_loop.tools.reply.china_now",
                return_value=NOW,
            ),
            patch(
                "qqbot.services.agent_loop.tools.reply.new_event_id",
                return_value="R1",
            ),
        ):
            outcome = await ReplyTool().run(
                {
                    "action": "upsert",
                    "targets": [
                        {"message_id": "M1", "points": ["回答问题"]}
                    ],
                    "gist": {"intent": "解释清楚", "facts": ["事实 A"]},
                    "hold_seconds": 8,
                },
                **self._context(notify),
            )
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.result["reply_task_id"], "R1")
        self.assertEqual(outcome.result["revision"], 1)
        self.assertNotIn("message_id", outcome.result)
        payload = append.await_args.kwargs["payload"]
        self.assertEqual(payload["flush_at"], (NOW + timedelta(seconds=8)).isoformat())
        notify.assert_awaited_once()

    async def test_merge_extends_but_never_passes_initial_hard_deadline(self) -> None:
        current = ReplyTaskState(
            reply_task_id="R1",
            scope_key="group:100",
            revision=1,
            state="open",
            created_at=NOW - timedelta(seconds=85),
            updated_at=NOW - timedelta(seconds=5),
            flush_at=NOW + timedelta(seconds=2),
            hard_deadline=NOW + timedelta(seconds=5),
            mode="compose",
            targets=[{"message_id": "M1", "points": ["A"]}],
            gist={"intent": "答复", "facts": ["F1"]},
            verbatim_messages=[],
            latest_event_id="E1",
            source_tool_call_event_id="TC1",
            correlation_id="CID",
        )
        notify = AsyncMock(side_effect=RuntimeError("timer unavailable"))
        with (
            patch(
                "qqbot.services.agent_loop.tools.reply.find_upsert_for_tool_call",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "qqbot.services.agent_loop.tools.reply.load_open_reply_task",
                new=AsyncMock(return_value=current),
            ),
            patch(
                "qqbot.services.agent_loop.tools.reply.append_upsert",
                new=AsyncMock(return_value="E2"),
            ) as append,
            patch(
                "qqbot.services.agent_loop.tools.reply.china_now",
                return_value=NOW,
            ),
        ):
            outcome = await ReplyTool().run(
                {
                    "action": "upsert",
                    "reply_task_id": "R1",
                    "expected_revision": 1,
                    "targets": [
                        {"message_id": "M1", "points": ["B"]}
                    ],
                    "gist": {"facts": ["F2"]},
                    "hold_seconds": 90,
                },
                **self._context(notify),
            )
        self.assertTrue(outcome.ok)
        payload = append.await_args.kwargs["payload"]
        self.assertEqual(payload["revision"], 2)
        self.assertEqual(payload["flush_at"], current.hard_deadline.isoformat())
        self.assertEqual(payload["gist"]["facts"], ["F1", "F2"])
        self.assertEqual(payload["targets"][0]["points"], ["A", "B"])


class ReplyTaskFoldTests(unittest.TestCase):
    def test_pending_claim_and_flush_are_distinct_states(self) -> None:
        upsert = _event(
            "E_UPSERT",
            "agent.reply_task_upserted",
            _upsert_payload(),
            causation_id="E_TOOL_CALL",
        )
        claimed = _event(
            "E_CLAIM",
            "runtime.reply_flush_claimed",
            {"reply_task_id": "R1", "revision": 1},
            seconds=1,
        )
        flushed = _event(
            "E_FLUSH",
            "runtime.reply_flushed",
            {
                "reply_task_id": "R1",
                "revision": 1,
                "status": "sent",
                "message_ids": [123],
            },
            seconds=2,
        )

        pending = _fold_rows([upsert])["R1"]
        self.assertEqual(pending.state, "open")
        self.assertEqual(pending.latest_event_id, "E_UPSERT")
        self.assertEqual(pending.source_tool_call_event_id, "E_TOOL_CALL")
        self.assertEqual(_fold_rows([upsert, claimed])["R1"].state, "claimed")
        self.assertEqual(
            _fold_rows([upsert, claimed, flushed])["R1"].state, "sent"
        )

    def test_new_revision_reopens_same_task_with_latest_source(self) -> None:
        first = _event(
            "E1",
            "agent.reply_task_upserted",
            _upsert_payload(),
            causation_id="TC1",
        )
        second_payload = _upsert_payload(2)
        second_payload["gist"]["facts"].append("事实 B")
        second = _event(
            "E2",
            "agent.reply_task_upserted",
            second_payload,
            causation_id="TC2",
            seconds=1,
        )
        state = _fold_rows([first, second])["R1"]
        self.assertEqual(state.state, "open")
        self.assertEqual(state.revision, 2)
        self.assertEqual(state.source_tool_call_event_id, "TC2")


class MergeContractTests(unittest.TestCase):
    def test_targets_merge_by_message_and_dedupe_points(self) -> None:
        merged = merge_targets(
            [{"message_id": "M1", "sender_qq": "1", "points": ["A"]}],
            [
                {"message_id": "M1", "sender_qq": "1", "points": ["A", "B"]},
                {"message_id": "M2", "sender_qq": "2", "points": ["C"]},
            ],
        )
        self.assertEqual(merged[0]["points"], ["A", "B"])
        self.assertEqual(merged[1]["message_id"], "M2")

    def test_gist_keeps_old_intent_and_merges_fact_lists(self) -> None:
        merged = merge_gist(
            {"intent": "旧意图", "facts": ["A"], "avoid": ["X"]},
            {"facts": ["A", "B"], "avoid": ["Y"]},
        )
        self.assertEqual(merged["intent"], "旧意图")
        self.assertEqual(merged["facts"], ["A", "B"])
        self.assertEqual(merged["avoid"], ["X", "Y"])


class ReplyerOutputTests(unittest.TestCase):
    def test_allows_ordered_chat_and_one_saved_meme(self) -> None:
        value = {
            "messages": [
                {
                    "kind": "chat",
                    "content": [{"type": "text", "data": {"text": "先说一句"}}],
                },
                {"kind": "meme", "image_hash": HASH_A},
                {
                    "kind": "chat",
                    "content": [{"type": "text", "data": {"text": "再补一句"}}],
                },
            ],
            "empty_reason": None,
        }
        parsed = _parse_output(json.dumps(value, ensure_ascii=False), {HASH_A})
        self.assertEqual(
            [item["kind"] for item in parsed["messages"]],
            ["chat", "meme", "chat"],
        )

    def test_normalizes_flat_segments_and_code_fence(self) -> None:
        """Gemini 系模型的真实漂移形态（2026-07-22 线上快照）：输出包 ```json
        围栏 + 段字段拍平到顶层。解析层无损归一成 OneBot data 包装，执行器
        preflight 的严格校验保持不变。"""
        raw = (
            "```json\n"
            '{"messages":[{"kind":"chat","content":['
            '{"type":"reply","id":"1115629605"},'
            '{"type":"text","text":"在呢，有事就直接说"}]}],"empty_reason":null}\n'
            "```"
        )
        parsed = _parse_output(raw, set())
        self.assertEqual(
            parsed["messages"][0]["content"],
            [
                {"type": "reply", "data": {"id": "1115629605"}},
                {"type": "text", "data": {"text": "在呢，有事就直接说"}},
            ],
        )

    def test_normalizes_reply_message_id_alias(self) -> None:
        value = {
            "messages": [
                {
                    "kind": "chat",
                    "content": [
                        {"type": "reply", "data": {"message_id": "840063058"}},
                        {"type": "at", "qq": "10001"},
                    ],
                }
            ],
            "empty_reason": None,
        }
        parsed = _parse_output(json.dumps(value, ensure_ascii=False), set())
        self.assertEqual(
            parsed["messages"][0]["content"],
            [
                {"type": "reply", "data": {"id": "840063058"}},
                {"type": "at", "data": {"qq": "10001"}},
            ],
        )

    def test_fence_without_closing_line_still_parses(self) -> None:
        raw = (
            "```json\n"
            '{"messages":[{"kind":"chat","content":'
            '[{"type":"text","data":{"text":"好"}}]}],"empty_reason":null}'
        )
        parsed = _parse_output(raw, set())
        self.assertEqual(
            parsed["messages"][0]["content"],
            [{"type": "text", "data": {"text": "好"}}],
        )

    def test_unrecognized_segment_shapes_pass_through_untouched(self) -> None:
        """归一只处理已知漂移；其余坏形态原样透传，由执行器严格校验
        fail loudly，不在解析层静默吞掉。"""
        content = [
            {"type": "image", "data": {"file": "x"}},
            {"type": "text", "data": "hello"},
        ]
        value = {
            "messages": [{"kind": "chat", "content": content}],
            "empty_reason": None,
        }
        parsed = _parse_output(json.dumps(value), set())
        self.assertEqual(parsed["messages"][0]["content"], content)

    def test_rejects_unknown_or_second_meme(self) -> None:
        unknown = {
            "messages": [{"kind": "meme", "image_hash": "cd" * 32}],
            "empty_reason": None,
        }
        with self.assertRaisesRegex(ValueError, "unknown meme"):
            _parse_output(json.dumps(unknown), {HASH_A})

        duplicate = {
            "messages": [
                {"kind": "meme", "image_hash": HASH_A},
                {"kind": "meme", "image_hash": HASH_A},
            ],
            "empty_reason": None,
        }
        with self.assertRaisesRegex(ValueError, "at most one meme"):
            _parse_output(json.dumps(duplicate), {HASH_A})

    def test_empty_reply_requires_reason(self) -> None:
        with self.assertRaisesRegex(ValueError, "empty output requires"):
            _parse_output('{"messages":[],"empty_reason":null}', set())

    def test_voice_lives_in_replyer_not_registered_tool_docs(self) -> None:
        prompt = _build_system_prompt()
        self.assertIn("小奏", prompt)
        self.assertNotIn("小奏", build_default_registry().usage_docs("group"))

    def test_voice_card_home_is_prompts_voice_md(self) -> None:
        """角色卡 2026-07-19 迁至 prompts/voice.md（唯一权威来源）；已下架的
        send_message.md 不再承载人格，防止两处副本漂移。"""
        from qqbot.services.agent_loop import replyer as replyer_mod

        voice_text = replyer_mod._VOICE_PATH.read_text(encoding="utf-8")
        self.assertIn("小奏", voice_text)
        self.assertIn("那个特殊的人", voice_text)
        legacy = (
            replyer_mod._VOICE_PATH.parent.parent / "tools" / "send_message.md"
        ).read_text(encoding="utf-8")
        self.assertNotIn("你叫小奏", legacy)

    def test_missing_voice_file_fails_loudly(self) -> None:
        """voice.md 缺失 = 部署损坏：组稿必须失败（final 记 failed 并唤醒
        Planner），绝不静默降级成无人格腔——那是最难被发现的坏法。"""
        from pathlib import Path

        from qqbot.services.agent_loop import replyer as replyer_mod

        original = replyer_mod._VOICE_PATH
        replyer_mod._VOICE_PATH = Path("/nonexistent/voice-card.md")
        try:
            with self.assertRaises(replyer_mod.ReplyerError):
                _build_system_prompt()
        finally:
            replyer_mod._VOICE_PATH = original


if __name__ == "__main__":
    unittest.main()

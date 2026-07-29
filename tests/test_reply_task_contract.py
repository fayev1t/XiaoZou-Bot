"""ReplyTask / Replyer 的核心合同测试。

钉住五个边界：reply 取代 send_message；**普通发言的参数只剩 brief +
hold_seconds，`action` 省略即追加**（2026-07-25，撤稿/逐字直发仍留在同一个工具
的 action 分支里）；草稿折叠与最终事实分离；授权 append-only、最新一次获胜
（2026-07-24 待办#19 取代原"合稿去重"）；Replyer 一次输出可含多条文本和至多
一张已收藏 meme。
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from qqbot.services.agent_loop.decision import (
    DecisionContext,
    ImageRef,
    TimelineItem,
)
from qqbot.services.agent_loop import reply_task as reply_task_module
from qqbot.services.agent_loop.reply_task import ReplyTaskState, _fold_rows
from qqbot.services.agent_loop.replyer import (
    Replyer,
    _build_system_prompt,
    _parse_output,
)
from qqbot.services.agent_loop.tools import build_default_registry
from qqbot.services.agent_loop.tools.reply import ReplyTool

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 7, 19, 12, 0, tzinfo=TZ)
HASH_A = "ab" * 32
_CHAT = [{"type": "text", "data": {"text": "精确文本"}}]


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
        "brief": "李四@我提问，解释清楚；事实 A 必须准确",
        "verbatim_messages": [],
    }


class RegistryBoundaryTests(unittest.TestCase):
    def test_reply_replaces_send_message_and_meme_send_is_removed(self) -> None:
        registry = build_default_registry()
        self.assertIn("reply", registry.names())
        self.assertNotIn("send_message", registry.names())
        # 2026-07-25 改名：`meme` → `meme_collection`（工具面仍无 send 动作）。
        meme_tool = registry.get("meme_collection")
        self.assertIsNotNone(meme_tool)
        meme_schema = meme_tool.arguments_schema  # type: ignore[union-attr]
        self.assertEqual(
            meme_schema["properties"]["action"]["enum"],
            ["save", "delete", "recaption"],
        )

    def test_speaking_surface_stays_one_tool(self) -> None:
        """撤稿与逐字直发**不拆成独立工具**（2026-07-25 评估后否决）。

        工具是目录级的东西：每注册一个，Planner 每拍都要读它的 catalog 条目
        和整段 usage 文档。代价不只是 prompt 体积——verbatim 本该是"Replyer
        挂了才走"的逃生路径，升格成与 reply 平级的工具就把它的显著性抬到与
        日常发言同等，等于邀请模型绕过 Replyer 和角色卡说话。
        """
        names = build_default_registry().names()
        self.assertIn("reply", names)
        for retired in ("cancel_reply", "say_verbatim"):
            self.assertNotIn(retired, names)

    def test_ordinary_speech_needs_only_brief_and_hold(self) -> None:
        """字段收敛的硬钉子：targets/gist 九个槽位没有独立程序语义，却用
        additionalProperties:false 把 Planner 能表达的辅助维度封死，还诱导
        它把一段判读切成七份填。收敛成一个自由文本 brief 后，折叠态把最新
        完整 brief 直送 Replyer。

        `action` 仍在，但**省略即追加**——`upsert` 这个取值取消了，普通发言
        不必先声明一遍状态机操作。
        """
        schema = ReplyTool.arguments_schema
        self.assertEqual(
            sorted(schema["properties"]),
            ["action", "brief", "hold_seconds", "messages", "reply_task_id"],
        )
        self.assertEqual(schema["required"], [])
        self.assertEqual(schema["properties"]["action"]["enum"], ["cancel", "verbatim"])
        # 省略 action 时 brief + hold_seconds 才是必填（allOf 的第一条分支）。
        self.assertEqual(
            schema["allOf"][0],
            {
                "if": {"not": {"required": ["action"]}},
                "then": {
                    "required": ["brief", "hold_seconds"],
                    "not": {
                        "anyOf": [
                            {"required": ["messages"]},
                            {"required": ["reply_task_id"]},
                        ]
                    },
                },
            },
        )
        self.assertEqual(len(schema["allOf"]), 3)


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
                    "brief": "李四@我提问，直接给结论；事实 A 必须准确",
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
        self.assertEqual(payload["mode"], "compose")
        self.assertEqual(
            payload["brief"], "李四@我提问，直接给结论；事实 A 必须准确"
        )
        notify.assert_awaited_once()

    @staticmethod
    def _open_task(
        *,
        mode: str = "compose",
        flush_offset: int = 40,
        deadline_offset: int = 60,
    ) -> ReplyTaskState:
        return ReplyTaskState(
            reply_task_id="R1",
            scope_key="group:100",
            revision=1,
            state="open",
            created_at=NOW - timedelta(seconds=30),
            updated_at=NOW - timedelta(seconds=5),
            flush_at=NOW + timedelta(seconds=flush_offset),
            hard_deadline=NOW + timedelta(seconds=deadline_offset),
            mode=mode,
            brief="" if mode == "verbatim" else "旧判读",
            verbatim_messages=[],
            latest_event_id="E1",
            source_tool_call_event_id="TC1",
            correlation_id="CID",
        )

    async def _append(
        self, arguments: dict, current: ReplyTaskState | None
    ) -> tuple[object, AsyncMock]:
        notify = AsyncMock()
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
            outcome = await ReplyTool().run(arguments, **self._context(notify))
        return outcome, append

    async def test_append_needs_no_id_and_does_not_merge_old_content(self) -> None:
        """append-only（待办#19）：不传 reply_task_id / expected_revision 也
        能续在同一份稿上，且事件里只留**本次授权原文**——旧判读不再被并进来
        （旧 merge 只增不减，撤不掉写错的判读与事实）。"""
        outcome, append = await self._append(
            {"brief": "改成先反问一句；F2 才是对的", "hold_seconds": 10},
            self._open_task(),
        )
        self.assertTrue(outcome.ok)
        payload = append.await_args.kwargs["payload"]
        self.assertEqual(payload["reply_task_id"], "R1")
        self.assertEqual(payload["revision"], 2)
        self.assertEqual(payload["brief"], "改成先反问一句；F2 才是对的")

    async def test_newest_hold_wins_and_may_shorten(self) -> None:
        """最新一次调用的 hold 直接获胜——旧实现的 max() 只能延长，模型
        发现"他说完了"也收不回来。"""
        outcome, append = await self._append(
            {"brief": "他说完了，直接回", "hold_seconds": 3},
            self._open_task(flush_offset=40),
        )
        self.assertTrue(outcome.ok)
        payload = append.await_args.kwargs["payload"]
        self.assertEqual(
            payload["flush_at"], (NOW + timedelta(seconds=3)).isoformat()
        )

    async def test_hard_deadline_is_inherited_and_caps_the_hold(self) -> None:
        """hard_deadline 自首次创建起算、不随 append 滑动，且是 flush_at 的
        硬上界。"""
        current = self._open_task(flush_offset=2, deadline_offset=5)
        outcome, append = await self._append(
            {"brief": "他还在打字，再等等", "hold_seconds": 90},
            current,
        )
        self.assertTrue(outcome.ok)
        payload = append.await_args.kwargs["payload"]
        self.assertEqual(payload["hard_deadline"], current.hard_deadline.isoformat())
        self.assertEqual(payload["created_at"], current.created_at.isoformat())
        self.assertEqual(payload["flush_at"], current.hard_deadline.isoformat())

    async def test_hold_seconds_is_required_with_no_default(self) -> None:
        """等多久是每拍现场判断的语义，没有默认值——旧的 default 0 等于把
        合并窗口整个关掉。"""
        outcome, append = await self._append({"brief": "判读"}, None)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "invalid_arguments")
        self.assertEqual(
            outcome.extra.get("reason_code"), "missing_hold_seconds"
        )
        append.assert_not_awaited()

    async def test_verbatim_draft_is_exclusive(self) -> None:
        """verbatim 绕过 Replyer 直发，没有"综合多条授权"可言：挂着一份就
        拒绝后续追加，只能先 cancel。"""
        outcome, append = await self._append(
            {"brief": "判读", "hold_seconds": 5},
            self._open_task(mode="verbatim"),
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "reply_task_locked")
        append.assert_not_awaited()

    async def test_verbatim_cannot_join_a_pending_compose_draft(self) -> None:
        """反方向同样拦：逐字语义不该被别的授权改写，也不该改写别人的。"""
        outcome, append = await self._append(
            {"action": "verbatim", "messages": [{"content": _CHAT}]},
            self._open_task(),
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "reply_task_locked")
        append.assert_not_awaited()

    async def test_verbatim_creates_a_fresh_draft_flushing_now(self) -> None:
        """hold 在 verbatim 上可省、默认 0：等待窗口的用处是让 Replyer 在
        flush 时折进新消息，而逐字直发不经 Replyer、字节已定死。"""
        outcome, append = await self._append(
            {"action": "verbatim", "messages": [{"content": _CHAT}]}, None
        )
        self.assertTrue(outcome.ok)
        payload = append.await_args.kwargs["payload"]
        self.assertEqual(payload["mode"], "verbatim")
        self.assertEqual(payload["flush_at"], NOW.isoformat())
        self.assertEqual(payload["verbatim_messages"], [{"content": _CHAT}])
        self.assertEqual(payload["brief"], "")

    async def test_verbatim_rejects_empty_and_oversized_batches(self) -> None:
        outcome, append = await self._append(
            {"action": "verbatim", "messages": []}, None
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.extra.get("reason_code"), "empty_messages")
        outcome, append = await self._append(
            {"action": "verbatim", "messages": [{"content": _CHAT}] * 5}, None
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.extra.get("reason_code"), "too_many_messages")
        append.assert_not_awaited()


def _tool_context() -> dict:
    return {
        "scope_key": "group:100",
        "session_factory": object(),
        "correlation_id": "CID",
        "tool_call_event_id": "E_TOOL_CALL",
    }


class ReplyArgumentSurfaceTests(unittest.IsolatedAsyncioTestCase):
    """2026-07-25 字段收敛：brief 一个自由文本顶掉 targets/gist 九个槽位，
    `action` 从必填判别式变成可选分支（省略即追加，`upsert` 取值取消）。
    旧形态 fail loudly，不静默丢弃——静默是最坏的：Planner 会以为判读送到了
    Replyer，而从 <result> 上看不出任何异常。"""

    async def _run(self, arguments: dict) -> object:
        with patch(
            "qqbot.services.agent_loop.tools.reply.find_upsert_for_tool_call",
            new=AsyncMock(return_value=None),
        ):
            return await ReplyTool().run(arguments, **_tool_context())

    async def test_retired_argument_shapes_fail_with_migration_hint(self) -> None:
        cases = {
            "targets": "targets_gist_replaced_by_brief",
            "gist": "targets_gist_replaced_by_brief",
            "points": "targets_gist_replaced_by_brief",
            "mode": "mode_replaced_by_action",
            "verbatim_messages": "verbatim_messages_renamed_to_messages",
            "expected_revision": "expected_revision_removed",
        }
        for key, reason_code in cases.items():
            with self.subTest(key=key):
                outcome = await self._run(
                    {"brief": "判读", "hold_seconds": 5, key: "whatever"}
                )
                self.assertFalse(outcome.ok)
                self.assertEqual(outcome.error_kind, "invalid_arguments")
                self.assertEqual(outcome.extra.get("reason_code"), reason_code)

    async def test_upsert_action_is_gone_with_a_pointed_hint(self) -> None:
        """2026-07-24 改 append-only 之后 upsert 就名不副实了（那之前它真是
        upsert：带 id + expected_revision 做 CAS 合并）。留着只是让每次正常
        发言都先声明一遍状态机操作。"""
        outcome = await self._run(
            {"action": "upsert", "brief": "判读", "hold_seconds": 5}
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.extra.get("reason_code"), "upsert_removed")

    async def test_brief_must_be_a_nonempty_string(self) -> None:
        for brief in (None, "", "   ", ["判读"]):
            with self.subTest(brief=brief):
                arguments: dict = {"hold_seconds": 5}
                if brief is not None:
                    arguments["brief"] = brief
                outcome = await self._run(arguments)
                self.assertFalse(outcome.ok)
                self.assertEqual(outcome.extra.get("reason_code"), "bad_brief")

    async def test_brief_and_messages_do_not_cross_branches(self) -> None:
        """两条分支的字段互不容忍：verbatim 绕过 Replyer，写了 brief 也没有
        任何读者，静默丢掉正是最难被发现的坏法。"""
        outcome = await self._run(
            {"action": "verbatim", "brief": "判读", "messages": [{"content": _CHAT}]}
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(
            outcome.extra.get("reason_code"), "brief_not_applicable"
        )
        outcome = await self._run(
            {"brief": "判读", "hold_seconds": 5, "messages": [{"content": _CHAT}]}
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(
            outcome.extra.get("reason_code"), "messages_need_verbatim"
        )

    async def test_reply_task_id_outside_cancel_is_rejected(self) -> None:
        """带 id 来追加 = 还在用旧的"指名改某一份稿"心智模型。静默忽略会让它
        以为精确命中了某份稿，实际是往当前 open 的那份上追加。"""
        outcome = await self._run(
            {"brief": "判读", "hold_seconds": 5, "reply_task_id": "R1"}
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(
            outcome.extra.get("reason_code"), "reply_task_id_needs_cancel"
        )

    async def test_explicit_null_action_is_not_treated_as_omission(self) -> None:
        outcome = await self._run(
            {"action": None, "brief": "判读", "hold_seconds": 5}
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.extra.get("reason_code"), "bad_action")

    async def test_non_object_arguments_fail_loudly(self) -> None:
        outcome = await ReplyTool().run(  # type: ignore[arg-type]
            ["brief", "判读"], **_tool_context()
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(
            outcome.extra.get("reason_code"), "arguments_not_object"
        )

    async def test_unknown_top_level_argument_fails_loudly(self) -> None:
        outcome = await self._run(
            {"brief": "判读", "hold_seconds": 5, "hold_second": 5}
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(
            outcome.extra.get("reason_code"), "unexpected_argument"
        )

    async def test_cancel_rejects_fields_from_other_branches(self) -> None:
        cases = {
            "brief": "判读",
            "hold_seconds": 5,
            "messages": [{"content": _CHAT}],
        }
        for key, value in cases.items():
            with self.subTest(key=key):
                outcome = await self._run({"action": "cancel", key: value})
                self.assertFalse(outcome.ok)
                self.assertEqual(
                    outcome.extra.get("reason_code"),
                    "cancel_arguments_not_applicable",
                )

    async def test_verbatim_message_rejects_extra_fields(self) -> None:
        outcome = await self._run(
            {
                "action": "verbatim",
                "messages": [{"content": _CHAT, "caption": "ignored before"}],
            }
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(
            outcome.extra.get("reason_code"),
            "unexpected_message_argument",
        )


class ReplyCancelActionTests(unittest.IsolatedAsyncioTestCase):
    """`action="cancel"` 撤掉待发的那份稿，通常无其它参数。"""

    @staticmethod
    def _open_task() -> ReplyTaskState:
        return ReplyTaskState(
            reply_task_id="R1",
            scope_key="group:100",
            revision=3,
            state="open",
            created_at=NOW,
            updated_at=NOW,
            flush_at=NOW + timedelta(seconds=10),
            hard_deadline=NOW + timedelta(seconds=90),
            mode="compose",
            brief="待撤稿",
            verbatim_messages=[],
            latest_event_id="E1",
            source_tool_call_event_id="TC1",
            correlation_id="CID",
        )

    async def _run(self, arguments: dict, current: ReplyTaskState | None):
        cancel = AsyncMock(return_value="E_CANCEL")
        with (
            patch(
                "qqbot.services.agent_loop.tools.reply."
                "find_cancel_for_tool_call",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "qqbot.services.agent_loop.tools.reply.load_open_reply_task",
                new=AsyncMock(return_value=current),
            ),
            patch(
                "qqbot.services.agent_loop.tools.reply.append_cancel",
                new=cancel,
            ),
        ):
            outcome = await ReplyTool().run(arguments, **_tool_context())
        return outcome, cancel

    async def test_bare_cancel_withdraws_the_pending_draft(self) -> None:
        outcome, cancel = await self._run(
            {"action": "cancel"}, self._open_task()
        )
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.result["reply_task_id"], "R1")
        self.assertEqual(outcome.result["state"], "cancelled")
        cancel.assert_awaited_once()

    async def test_stale_id_assertion_fails_instead_of_cancelling_another(
        self,
    ) -> None:
        """可选断言的存在理由：模型以为在撤 R1（其实已 flush），当前 open 的
        是 R2——没有断言就会静默撤错那一份。"""
        outcome, cancel = await self._run(
            {"action": "cancel", "reply_task_id": "R_OLD"}, self._open_task()
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "reply_task_not_found")
        cancel.assert_not_awaited()

    async def test_nothing_pending_is_a_readable_failure(self) -> None:
        outcome, cancel = await self._run({"action": "cancel"}, None)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "reply_task_not_found")
        cancel.assert_not_awaited()


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
        self.assertEqual(
            pending.brief, "李四@我提问，解释清楚；事实 A 必须准确"
        )
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
        second_payload["brief"] = "补一个事实 B"
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
        self.assertEqual(state.brief, "补一个事实 B")
        self.assertEqual(state.source_tool_call_event_id, "TC2")

    def test_malformed_brief_is_not_stringified_into_authorization(self) -> None:
        payload = _upsert_payload()
        payload["brief"] = ["不能把 repr 当授权"]
        state = _fold_rows(
            [_event("E_BAD", "agent.reply_task_upserted", payload)]
        )["R1"]
        self.assertEqual(state.brief, "")


class LatestAuthorizationContractTests(unittest.TestCase):
    """程序侧不再做任何合并计算（2026-07-24，待办#19）。

    原 MergeContractTests 钉的是 merge_targets/merge_gist 的并集语义，而那
    正是"撤不掉已授权的 target / 写错的 fact"的来源。事件仍逐条原样入库，
    但当前有效授权明确折叠为最新 revision 的完整 brief；旧 revision 只供审计，
    Replyer 不再做隐式语义 merge。
    """

    def test_merge_helpers_are_gone(self) -> None:
        self.assertFalse(hasattr(reply_task_module, "merge_targets"))
        self.assertFalse(hasattr(reply_task_module, "merge_gist"))
        self.assertFalse(hasattr(reply_task_module, "_dedupe_strings"))

    def test_fold_takes_schedule_and_complete_brief_from_latest_upsert(self) -> None:
        """最新 revision 同时决定调度与完整授权；省略旧内容就是撤回旧内容。

        brief 必须随折叠态进入 Replyer，不能只存在于有终态竞态、且会被裁剪的
        通用 timeline tool-call 行。
        """
        first = _event(
            "E1", "agent.reply_task_upserted", _upsert_payload(), causation_id="TC1"
        )
        second_payload = _upsert_payload(2)
        second_payload["brief"] = "改主意了，换个角度"
        second_payload["flush_at"] = (NOW + timedelta(seconds=3)).isoformat()
        second = _event(
            "E2",
            "agent.reply_task_upserted",
            second_payload,
            causation_id="TC2",
            seconds=1,
        )
        state = _fold_rows([first, second])["R1"]
        self.assertEqual(state.revision, 2)
        self.assertEqual(state.flush_at, NOW + timedelta(seconds=3))
        self.assertEqual(state.brief, "改主意了，换个角度")
        self.assertNotIn("事实 A", state.brief)
        self.assertFalse(hasattr(state, "targets"))
        self.assertFalse(hasattr(state, "gist"))


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


class _CapturingLLM:
    """记录 ainvoke 收到的 messages，返回最小合法组稿 JSON。"""

    def __init__(self) -> None:
        self.messages: list | None = None

    async def ainvoke(self, messages: list) -> SimpleNamespace:
        self.messages = messages
        return SimpleNamespace(
            content=json.dumps(
                {
                    "messages": [
                        {
                            "kind": "chat",
                            "content": [
                                {"type": "text", "data": {"text": "好"}}
                            ],
                        }
                    ],
                    "empty_reason": None,
                }
            )
        )


class ReplyerMultimodalTests(unittest.TestCase):
    """compose 的多模态 content 合同（2026-07-22 Replyer 与 Planner 同等看图）：
    有图 → content 为 [文本块, *图块] 且文本块即 XML 信封（replyer-input）；
    无图 → content 保持纯字符串。"""

    @classmethod
    def setUpClass(cls) -> None:
        # 本地无 langchain 时桩替 message 类；服务器上真模块存在则原样使用。
        try:
            import langchain_core.messages  # noqa: F401
        except Exception:
            messages_mod = types.ModuleType("langchain_core.messages")

            class _Message:
                def __init__(self, content: object) -> None:
                    self.content = content

            messages_mod.SystemMessage = _Message  # type: ignore[attr-defined]
            messages_mod.HumanMessage = _Message  # type: ignore[attr-defined]
            root = sys.modules.setdefault(
                "langchain_core", types.ModuleType("langchain_core")
            )
            root.messages = messages_mod  # type: ignore[attr-defined]
            sys.modules["langchain_core.messages"] = messages_mod

    @staticmethod
    def _compose_task() -> ReplyTaskState:
        return ReplyTaskState(
            reply_task_id="R1",
            scope_key="group:100",
            revision=1,
            state="claimed",
            created_at=NOW,
            updated_at=NOW,
            flush_at=NOW,
            hard_deadline=NOW + timedelta(seconds=90),
            mode="compose",
            brief="李四@我提问，解释清楚",
            verbatim_messages=[],
            latest_event_id="E1",
            source_tool_call_event_id="TC1",
            correlation_id="CID",
        )

    def _run_compose(self, timeline: list[TimelineItem]) -> _CapturingLLM:
        llm = _CapturingLLM()
        context = DecisionContext(
            scope_key="group:100",
            correlation_id="CID",
            tick_seq=0,
            now=NOW,
            timeline=timeline,
        )
        with patch(
            "qqbot.services.agent_loop.replyer.should_snapshot",
            return_value=False,
        ):
            result = asyncio.run(
                Replyer(llm_client=llm).compose(
                    self._compose_task(), context, []
                )
            )
        self.assertEqual(result["messages"][0]["kind"], "chat")
        return llm

    def test_compose_never_attaches_image_blocks(self) -> None:
        """2026-07-28：Replyer 降级为纯文本模型。timeline 里带已落盘图片的
        消息**不再**让 content 变成多模态数组——图片内容以 ingest 期写好的
        desc= 属性随 render 文本进 prompt（见 image_description 模块）。"""
        item = TimelineItem(
            event_id="E1",
            occurred_at=NOW,
            kind="message",
            render=f'<message><image hash="{HASH_A}" desc="一只猫"/></message>',
            images=[
                ImageRef(
                    file_hash=HASH_A,
                    local_path="/nonexistent/never-read",
                    mime="image/png",
                )
            ],
        )
        llm = self._run_compose([item])
        assert llm.messages is not None
        content = llm.messages[1].content
        self.assertIsInstance(content, str)
        self.assertTrue(content.startswith("<replyer-input "))
        # 图片语义只经 render 文本抵达，且没有任何 base64 载荷。
        self.assertIn('desc="一只猫"', content)
        self.assertNotIn("base64", content)

    def test_compose_without_images_keeps_plain_text_content(self) -> None:
        llm = self._run_compose([])
        assert llm.messages is not None
        content = llm.messages[1].content
        self.assertIsInstance(content, str)
        self.assertTrue(content.startswith("<replyer-input "))
        self.assertIn('reply_task_id="R1"', content)


if __name__ == "__main__":
    unittest.main()

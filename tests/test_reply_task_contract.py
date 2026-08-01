"""ReplyTask 的核心合同测试。

钉住五个边界：发言分两步（reply 起一段等待，send_messages 发送——2026-07-31
删除 Replyer）；**追加的参数只剩 hold_seconds 一个，`action` 省略即追加**
（2026-08-01 删除内容通道，撤稿仍留在同一个工具的 action 分支里）；等待折叠
与最终事实分离；append-only、最新一次获胜（2026-07-24 待办#19）；状态机
open → completed | cancelled，过期完成事件被折叠层拒绝。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from qqbot.services.agent_loop import reply_task as reply_task_module
from qqbot.services.agent_loop.reply_task import ReplyTaskState, _fold_rows
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
    }


class RegistryBoundaryTests(unittest.TestCase):
    def test_speaking_is_reply_plus_send_messages(self) -> None:
        """发言两步各占一个工具（2026-07-31 删除 Replyer）：reply 起一段
        等待，send_messages 发送；退役的单数 send_message 不得复活。"""
        registry = build_default_registry()
        self.assertIn("reply", registry.names())
        self.assertIn("send_messages", registry.names())
        self.assertNotIn("send_message", registry.names())
        # reply 不再覆盖工具批次的统一唤醒语义。
        self.assertFalse(hasattr(registry.get("reply"), "wake_policy"))
        # 2026-07-25 改名：`meme` → `meme_collection`（工具面仍无 send 动作）。
        meme_tool = registry.get("meme_collection")
        self.assertIsNotNone(meme_tool)
        meme_schema = meme_tool.arguments_schema  # type: ignore[union-attr]
        self.assertEqual(
            meme_schema["properties"]["action"]["enum"],
            ["save", "delete", "recaption"],
        )

    def test_cancel_stays_a_branch_not_a_tool(self) -> None:
        """撤稿**不拆成独立工具**（2026-07-25 评估后否决）：工具是目录级的
        东西，每注册一个，Planner 每拍都要读它的 catalog 条目和整段 usage
        文档，而撤稿的使用频率远撑不起那份显著性。

        `say_verbatim` 同样不得作为工具出现——逐字直发 2026-07-30 已整条
        删除；最终字句的唯一出口是 send_messages。
        """
        names = build_default_registry().names()
        self.assertIn("reply", names)
        for retired in ("cancel_reply", "say_verbatim"):
            self.assertNotIn(retired, names)

    def test_ordinary_speech_needs_only_hold_seconds(self) -> None:
        """2026-08-01 内容通道整条删除：普通分支**只剩 hold_seconds 一个
        参数**，工具面上不存在任何承载内容的字段。

        这条通道曾是 targets/gist 九槽位 → 自由文本 brief → analysis，一路
        收敛到无。删干净的理由是它已经没有第二个读者（Replyer 2026-07-31
        删除），留着只会把同一段话在时间线上渲染两遍，并且把 T 时刻的判读
        摆到 T+hold 的落笔现场——而那段窗口按设计就是局势还会变的窗口。

        `action` 仍在，但**省略即追加**——`upsert` 取值取消了，普通发言不必
        先声明一遍状态机操作；`verbatim` 也不在取值里（2026-07-30）。
        """
        schema = ReplyTool.arguments_schema
        self.assertEqual(
            sorted(schema["properties"]),
            ["action", "hold_seconds", "reply_task_id"],
        )
        self.assertEqual(schema["required"], [])
        self.assertEqual(schema["properties"]["action"]["enum"], ["cancel"])
        # 省略 action 时只有 hold_seconds 必填（allOf 第一条分支）。
        self.assertEqual(
            schema["allOf"][0],
            {
                "if": {"not": {"required": ["action"]}},
                "then": {
                    "required": ["hold_seconds"],
                    "not": {"required": ["reply_task_id"]},
                },
            },
        )
        self.assertEqual(len(schema["allOf"]), 2)
        # hold_seconds 的说明必须交代这段等待覆盖什么——它是这个工具现在
        # 唯一的判断点：我打这些字要多久 + 对方可能还没说完。
        description = schema["properties"]["hold_seconds"]["description"]
        self.assertIn("打出来", description)
        self.assertIn("继续发言", description)
        self.assertIn("无默认值", description)


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
                {"hold_seconds": 8}, **self._context(notify)
            )
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.result["reply_task_id"], "R1")
        self.assertEqual(outcome.result["revision"], 1)
        self.assertNotIn("message_id", outcome.result)
        payload = append.await_args.kwargs["payload"]
        self.assertEqual(payload["flush_at"], (NOW + timedelta(seconds=8)).isoformat())
        # 领域事件只有调度事实：内容通道 2026-08-01 整条删除，写入侧不得
        # 再悄悄留一个空字段占位。
        self.assertNotIn("analysis", payload)
        self.assertNotIn("brief", payload)
        notify.assert_awaited_once()

    @staticmethod
    def _open_task(
        *,
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

    async def test_append_needs_no_id_and_reuses_the_open_task(self) -> None:
        """append-only（待办#19）：不传 reply_task_id / expected_revision 也
        能续在同一段等待上，revision 自增。删掉内容通道之后这条追加只剩
        "把等待时机换成新的"这一件事。"""
        outcome, append = await self._append(
            {"hold_seconds": 10}, self._open_task()
        )
        self.assertTrue(outcome.ok)
        payload = append.await_args.kwargs["payload"]
        self.assertEqual(payload["reply_task_id"], "R1")
        self.assertEqual(payload["revision"], 2)
        self.assertEqual(
            payload["flush_at"], (NOW + timedelta(seconds=10)).isoformat()
        )

    async def test_newest_hold_wins_and_may_shorten(self) -> None:
        """最新一次调用的 hold 直接获胜——旧实现的 max() 只能延长，模型
        发现"他说完了"也收不回来。"""
        outcome, append = await self._append(
            {"hold_seconds": 3}, self._open_task(flush_offset=40)
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
        outcome, append = await self._append({"hold_seconds": 90}, current)
        self.assertTrue(outcome.ok)
        payload = append.await_args.kwargs["payload"]
        self.assertEqual(payload["hard_deadline"], current.hard_deadline.isoformat())
        self.assertEqual(payload["created_at"], current.created_at.isoformat())
        self.assertEqual(payload["flush_at"], current.hard_deadline.isoformat())

    async def test_hold_seconds_is_required_with_no_default(self) -> None:
        """等多久是每拍现场判断的语义，没有默认值——旧的 default 0 等于把
        等待窗口整个关掉。删掉内容通道后它是唯一的参数，空调用也必须失败，
        不能退化成"随便等一下"。"""
        outcome, append = await self._append({}, None)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "invalid_arguments")
        self.assertEqual(
            outcome.extra.get("reason_code"), "missing_hold_seconds"
        )
        append.assert_not_awaited()

    async def test_appending_onto_an_open_wait_is_never_locked(self) -> None:
        """scope 内只剩一段等待，独占约束与 reply_task_locked 随逐字直发一并
        删除（2026-07-30）：已有一段 open 等待时继续续期必须照常成功。"""
        outcome, append = await self._append({"hold_seconds": 5}, self._open_task())
        self.assertTrue(outcome.ok)
        append.assert_awaited_once()

    async def test_verbatim_action_no_longer_exists(self) -> None:
        """逐字直发 2026-07-30 删除：Planner 不再有任何写出可见字句的通路。
        旧调用不得静默降级成一次普通等待——那会让它以为自己定死的字句发出去
        了。"""
        for arguments in (
            {"action": "verbatim", "messages": [{"content": _CHAT}]},
            {"action": "verbatim", "hold_seconds": 5},
        ):
            with self.subTest(arguments=sorted(arguments)):
                outcome, append = await self._append(arguments, None)
                self.assertFalse(outcome.ok)
                self.assertEqual(outcome.error_kind, "invalid_arguments")
                self.assertEqual(
                    outcome.extra.get("reason_code"), "verbatim_removed"
                )
                append.assert_not_awaited()


def _tool_context() -> dict:
    return {
        "scope_key": "group:100",
        "session_factory": object(),
        "correlation_id": "CID",
        "tool_call_event_id": "E_TOOL_CALL",
    }


class ReplyArgumentSurfaceTests(unittest.IsolatedAsyncioTestCase):
    """2026-08-01 内容通道整条删除，只剩 hold_seconds；`action` 是可选分支
    （省略即追加，`upsert` 取值取消）。

    旧形态一律 fail loudly，不静默丢弃——静默是最坏的：Planner 会以为那段
    判读已经存进去了，而从 <result> 上看不出任何异常。"""

    async def _run(self, arguments: dict) -> object:
        with patch(
            "qqbot.services.agent_loop.tools.reply.find_upsert_for_tool_call",
            new=AsyncMock(return_value=None),
        ):
            return await ReplyTool().run(arguments, **_tool_context())

    async def test_retired_argument_shapes_fail_with_migration_hint(self) -> None:
        cases = {
            # 内容通道的历代形状（2026-08-01 整条删除）共用一个 reason_code：
            # 问题不是字段名写错了，是这个工具已经不收任何内容。
            "analysis": "content_removed",
            "brief": "content_removed",
            "targets": "content_removed",
            "gist": "content_removed",
            "points": "content_removed",
            "mode": "mode_removed",
            # 2026-07-30 逐字直发下线：两个载荷字段都指向同一条迁移说明，
            # 让模型看出是"这条路没有了"而不是字段名拼错了。
            "messages": "verbatim_removed",
            "verbatim_messages": "verbatim_removed",
            "expected_revision": "expected_revision_removed",
        }
        for key, reason_code in cases.items():
            with self.subTest(key=key):
                outcome = await self._run({"hold_seconds": 5, key: "whatever"})
                self.assertFalse(outcome.ok)
                self.assertEqual(outcome.error_kind, "invalid_arguments")
                self.assertEqual(outcome.extra.get("reason_code"), reason_code)

    async def test_content_rejection_points_at_the_two_step_flow(self) -> None:
        """迁移说明必须把人推回正确流程，而不只是说"这个字段没了"：只传
        hold_seconds，等到点了再用 send_messages 现场定措辞。"""
        outcome = await self._run({"analysis": "判读", "hold_seconds": 5})
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.extra.get("reason_code"), "content_removed")
        self.assertIn("hold_seconds", outcome.error_message)
        self.assertIn("send_messages", outcome.error_message)

    async def test_upsert_action_is_gone_with_a_pointed_hint(self) -> None:
        """2026-07-24 改 append-only 之后 upsert 就名不副实了（那之前它真是
        upsert：带 id + expected_revision 做 CAS 合并）。留着只是让每次正常
        发言都先声明一遍状态机操作。"""
        outcome = await self._run({"action": "upsert", "hold_seconds": 5})
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.extra.get("reason_code"), "upsert_removed")

    async def test_messages_still_fails_on_an_otherwise_valid_call(self) -> None:
        """`messages` 已随逐字直发删除，混进一次合法的等待调用里也不能被静默
        忽略——那等于 Planner 以为自己定死的字句发出去了。"""
        outcome = await self._run(
            {"hold_seconds": 5, "messages": [{"content": _CHAT}]}
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.extra.get("reason_code"), "verbatim_removed")

    async def test_reply_task_id_outside_cancel_is_rejected(self) -> None:
        """带 id 来追加 = 还在用旧的"指名改某一份稿"心智模型。静默忽略会让它
        以为精确命中了某一份，实际是往当前 open 的那段等待上追加。"""
        outcome = await self._run({"hold_seconds": 5, "reply_task_id": "R1"})
        self.assertFalse(outcome.ok)
        self.assertEqual(
            outcome.extra.get("reason_code"), "reply_task_id_needs_cancel"
        )

    async def test_explicit_null_action_is_not_treated_as_omission(self) -> None:
        outcome = await self._run({"action": None, "hold_seconds": 5})
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.extra.get("reason_code"), "bad_action")

    async def test_non_object_arguments_fail_loudly(self) -> None:
        outcome = await ReplyTool().run(  # type: ignore[arg-type]
            ["hold_seconds", 5], **_tool_context()
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(
            outcome.extra.get("reason_code"), "arguments_not_object"
        )

    async def test_unknown_top_level_argument_fails_loudly(self) -> None:
        outcome = await self._run({"hold_seconds": 5, "hold_second": 5})
        self.assertFalse(outcome.ok)
        self.assertEqual(
            outcome.extra.get("reason_code"), "unexpected_argument"
        )

    async def test_cancel_rejects_fields_from_other_branches(self) -> None:
        outcome = await self._run({"action": "cancel", "hold_seconds": 5})
        self.assertFalse(outcome.ok)
        self.assertEqual(
            outcome.extra.get("reason_code"),
            "cancel_arguments_not_applicable",
        )


class ReplyCancelActionTests(unittest.IsolatedAsyncioTestCase):
    """`action="cancel"` 撤掉当前那段等待，通常无其它参数。"""

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

    async def test_bare_cancel_withdraws_the_pending_wait(self) -> None:
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
    def test_completed_event_folds_open_task_terminal(self) -> None:
        """新链路状态机：flush_at 到达 → runtime.reply_task_completed 把
        open 折成 completed（terminal）。"""
        upsert = _event(
            "E_UPSERT",
            "agent.reply_task_upserted",
            _upsert_payload(),
            causation_id="E_TOOL_CALL",
        )
        completed = _event(
            "E_DONE",
            "runtime.reply_task_completed",
            {"reply_task_id": "R1", "revision": 1},
            causation_id="E_UPSERT",
            seconds=1,
        )
        pending = _fold_rows([upsert])["R1"]
        self.assertEqual(pending.state, "open")
        self.assertEqual(pending.flush_at, NOW + timedelta(seconds=10))
        self.assertEqual(pending.latest_event_id, "E_UPSERT")
        self.assertEqual(pending.source_tool_call_event_id, "E_TOOL_CALL")
        self.assertEqual(
            _fold_rows([upsert, completed])["R1"].state, "completed"
        )

    def test_stale_completed_is_rejected_by_the_fold(self) -> None:
        """§1.5：更低 revision 的 completed 输给了并发追加的新 upsert，不得
        把任务折成 completed；任务保持 open 等新 revision 自己的完成事件。"""
        first = _event(
            "E1", "agent.reply_task_upserted", _upsert_payload(), causation_id="TC1"
        )
        second = _event(
            "E2",
            "agent.reply_task_upserted",
            _upsert_payload(2),
            causation_id="TC2",
            seconds=1,
        )
        stale_completed = _event(
            "E_DONE",
            "runtime.reply_task_completed",
            {"reply_task_id": "R1", "revision": 1},
            causation_id="E1",
            seconds=2,
        )
        state = _fold_rows([first, second, stale_completed])["R1"]
        self.assertEqual(state.state, "open")
        self.assertEqual(state.revision, 2)

    def test_late_completed_does_not_resurrect_a_cancelled_task(self) -> None:
        upsert = _event(
            "E1", "agent.reply_task_upserted", _upsert_payload(), causation_id="TC1"
        )
        cancelled = _event(
            "E_CANCEL",
            "agent.reply_task_cancelled",
            {"reply_task_id": "R1", "revision": 1, "state": "cancelled"},
            seconds=1,
        )
        late_completed = _event(
            "E_DONE",
            "runtime.reply_task_completed",
            {"reply_task_id": "R1", "revision": 1},
            causation_id="E1",
            seconds=2,
        )
        state = _fold_rows([upsert, cancelled, late_completed])["R1"]
        self.assertEqual(state.state, "cancelled")

    def test_legacy_claim_and_flush_still_fold_for_upgrade(self) -> None:
        """升级兼容：旧链路的 claim/flush 只用于把升级前的任务折成历史
        terminal（新链路不写它们）；缺 reply_task_id 的 flushed（防御）
        不改任何任务状态。"""
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
        taskless_flushed = _event(
            "E_TASKLESS_FLUSH",
            "runtime.reply_flushed",
            {"status": "sent", "message_ids": [456], "sent_messages": []},
            causation_id="E_SEND_TOOL_CALL",
            seconds=3,
        )
        self.assertEqual(_fold_rows([upsert, claimed])["R1"].state, "claimed")
        self.assertEqual(
            _fold_rows([upsert, claimed, flushed])["R1"].state, "sent"
        )
        # 无 reply_task_id 的 flushed（防御）→ 不改任何任务状态。
        self.assertEqual(
            _fold_rows([upsert, taskless_flushed])["R1"].state, "open"
        )

    def test_new_revision_reopens_same_task_with_latest_source(self) -> None:
        first = _event(
            "E1",
            "agent.reply_task_upserted",
            _upsert_payload(),
            causation_id="TC1",
        )
        second_payload = _upsert_payload(2)
        second_payload["flush_at"] = (NOW + timedelta(seconds=25)).isoformat()
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
        self.assertEqual(state.flush_at, NOW + timedelta(seconds=25))
        self.assertEqual(state.source_tool_call_event_id, "TC2")

    def test_fold_state_carries_no_content_field(self) -> None:
        """2026-08-01 内容通道删除后，折叠态上不得再出现任何内容字段——留一
        个空字符串占位就等于给下游一条"这里本来该有话"的错误暗示。"""
        state = _fold_rows(
            [_event("E1", "agent.reply_task_upserted", _upsert_payload())]
        )["R1"]
        for gone in ("analysis", "brief", "targets", "gist"):
            self.assertFalse(hasattr(state, gone), gone)

    def test_legacy_content_keys_are_ignored_not_folded(self) -> None:
        """升级前落库的事件里还带着 analysis / brief。它们留在 append-only
        流里不改不删，但折叠层直接忽略——调度事实照常折出来，内容不复活。"""
        for legacy_key in ("analysis", "brief"):
            with self.subTest(legacy_key=legacy_key):
                payload = _upsert_payload()
                payload[legacy_key] = "升级前已经落库的判读"
                state = _fold_rows(
                    [_event("E_LEGACY", "agent.reply_task_upserted", payload)]
                )["R1"]
                self.assertEqual(state.state, "open")
                self.assertEqual(state.revision, 1)
                self.assertEqual(state.flush_at, NOW + timedelta(seconds=10))
                self.assertFalse(hasattr(state, legacy_key))


class LatestRevisionContractTests(unittest.TestCase):
    """程序侧不再做任何合并计算（2026-07-24，待办#19）。

    原 MergeContractTests 钉的是 merge_targets/merge_gist 的并集语义，而那
    正是"撤不掉已写下的 target / 写错的 fact"的来源。2026-08-01 删掉内容通道
    之后，连"要不要合并"这个问题都不存在了——latest-revision-wins 只作用于
    等到什么时候这一件事。
    """

    def test_merge_helpers_are_gone(self) -> None:
        self.assertFalse(hasattr(reply_task_module, "merge_targets"))
        self.assertFalse(hasattr(reply_task_module, "merge_gist"))
        self.assertFalse(hasattr(reply_task_module, "_dedupe_strings"))

    def test_fold_takes_schedule_from_latest_upsert(self) -> None:
        """最新 revision 决定调度，且能缩短——不是取 max。"""
        first = _event(
            "E1", "agent.reply_task_upserted", _upsert_payload(), causation_id="TC1"
        )
        second_payload = _upsert_payload(2)
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
        self.assertFalse(hasattr(state, "targets"))
        self.assertFalse(hasattr(state, "gist"))


class PersonaCardHomeTests(unittest.TestCase):
    """角色卡的居所与注入路径（2026-07-31 删除 Replyer 后重锚）。

    卡片唯一真相源是 prompts/planner.md 的 §你是谁 段（同日由 persona.md 并回
    根页——删除 Replyer 之后它只剩 Planner 一个消费者，切文件已无收益）；历史
    居所（tools/send_message.md Voice 节 → prompts/voice.md →
    prompts/replyer.md → prompts/persona.md）全部不复存在。工具用法文档不得再
    承载人格正文——两处副本必然漂移。
    """

    def test_card_reaches_planner_but_not_tool_docs(self) -> None:
        from qqbot.services.agent_loop.prompts.catalog import (
            render_system_prompt,
        )

        prompt = render_system_prompt("planner", scope="group")
        self.assertIn("小奏", prompt)
        self.assertNotIn("小奏", build_default_registry().usage_docs("group"))

    def test_card_home_is_the_planner_page(self) -> None:
        """旧居所都不得复活：replyer.md / voice.md / send_message.md 已随各自
        宿主删除，persona.md 并回根页，卡片只住 planner.md 页首。"""
        from qqbot.services.agent_loop.prompts.catalog import _PROMPTS_DIR

        page = (_PROMPTS_DIR / "planner.md").read_text(encoding="utf-8")
        self.assertIn("# 你是谁", page)
        self.assertIn("小奏", page)
        self.assertIn("最重要的人", page)
        self.assertFalse((_PROMPTS_DIR / "voice.md").exists())
        self.assertFalse((_PROMPTS_DIR / "replyer.md").exists())
        self.assertFalse((_PROMPTS_DIR / "persona.md").exists())
        self.assertFalse(
            (_PROMPTS_DIR.parent / "tools" / "send_message.md").exists()
        )

    def test_missing_card_file_fails_loudly(self) -> None:
        """角色卡所在文件（= Planner 根页）缺失 = 部署损坏：prompt 装配必须
        失败（llm_planner 兜底降级 idle），绝不静默渲染无人格腔——那是最难被
        发现的坏法。"""
        from qqbot.services.agent_loop.prompts import catalog

        original = catalog.CONSUMERS["planner"]
        catalog.CONSUMERS["planner"] = "__no_such_planner_page__.md"
        try:
            with self.assertRaises(Exception):
                catalog.render_system_prompt("planner", scope="group")
        finally:
            catalog.CONSUMERS["planner"] = original


if __name__ == "__main__":
    unittest.main()

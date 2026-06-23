"""Contract tests for the v2 projection layer.

Pure unit-level: every test calls Projector's staticmethods with a
hand-built list of _EventSnapshot fixtures; no DB and no nonebot required.

Contract sources:
- 任务与决策契约.md §2.3 (timeline scoping & shape)
- 任务与决策契约.md §4.2 (task folding via agent.task_* events)
- 任务与决策契约.md §5.1 (rendering rules)
- 任务与决策契约.md §5.2 (active_tasks: only pending/running)
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from qqbot.services.agent_loop.projection import Projector, _EventSnapshot

SHANGHAI = ZoneInfo("Asia/Shanghai")
BASE_TIME = datetime(2026, 5, 26, 14, 30, 0, tzinfo=SHANGHAI)


def _snap(
    *,
    type: str,
    payload: dict | None = None,
    event_id: str = "",
    scope: str = "group",
    group_id: int | None = 999,
    user_id: int | None = 222,
    visibility: str = "agent_visible",
    origin: str | None = None,
    correlation_id: str | None = None,
    seconds_offset: float = 0.0,
) -> _EventSnapshot:
    if origin is None:
        origin = type.split(".", 1)[0]
    return _EventSnapshot(
        event_id=event_id or f"E{int(seconds_offset * 1000)}",
        occurred_at=BASE_TIME + timedelta(seconds=seconds_offset),
        origin=origin,
        type=type,
        scope=scope,
        group_id=group_id,
        user_id=user_id,
        visibility=visibility,
        correlation_id=correlation_id,
        causation_id=None,
        payload=payload or {},
    )


class FoldTasksTests(unittest.TestCase):
    def test_task_created_yields_pending(self) -> None:
        evs = [
            _snap(
                type="agent.task_created",
                payload={
                    "task_id": "T1",
                    "description": "desc",
                    "related_tools": ["web_search"],
                },
            ),
        ]
        tasks = Projector.fold_tasks(evs, scope_key="group:999")
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].task_id, "T1")
        self.assertEqual(tasks[0].state, "pending")
        self.assertEqual(tasks[0].related_tools, ["web_search"])
        self.assertEqual(tasks[0].scope_key, "group:999")

    def test_task_state_changes_apply_in_order(self) -> None:
        evs = [
            _snap(type="agent.task_created", payload={"task_id": "T1"}, seconds_offset=0),
            _snap(
                type="agent.task_state_changed",
                payload={"task_id": "T1", "from_state": "pending", "to_state": "running"},
                seconds_offset=1,
            ),
        ]
        tasks = Projector.fold_tasks(evs, scope_key="group:1")
        self.assertEqual(tasks[0].state, "running")

    def test_done_and_failed_tasks_are_dropped_from_active(self) -> None:
        evs = [
            _snap(type="agent.task_created", payload={"task_id": "T1"}, seconds_offset=0),
            _snap(
                type="agent.task_state_changed",
                payload={"task_id": "T1", "to_state": "done"},
                seconds_offset=1,
            ),
            _snap(type="agent.task_created", payload={"task_id": "T2"}, seconds_offset=2),
        ]
        tasks = Projector.fold_tasks(evs, scope_key="group:1")
        ids = {t.task_id for t in tasks}
        self.assertEqual(ids, {"T2"})  # T1 done → dropped

    def test_pending_tool_call_ids_track_open_calls(self) -> None:
        evs = [
            _snap(type="agent.task_created", payload={"task_id": "T1"}, seconds_offset=0),
            _snap(
                type="agent.tool_called",
                payload={"task_id": "T1", "tool_call_id": "TC1", "tool_name": "x"},
                seconds_offset=1,
            ),
            _snap(
                type="agent.tool_called",
                payload={"task_id": "T1", "tool_call_id": "TC2", "tool_name": "y"},
                seconds_offset=2,
            ),
            _snap(
                type="agent.tool_result",
                payload={"tool_call_id": "TC1", "result": "ok"},
                seconds_offset=3,
            ),
        ]
        tasks = Projector.fold_tasks(evs, scope_key="group:1")
        self.assertEqual(tasks[0].pending_tool_call_ids, ["TC2"])


class FoldToolResultsTests(unittest.TestCase):
    def test_pending_when_no_result_yet(self) -> None:
        evs = [
            _snap(
                type="agent.tool_called",
                payload={"tool_call_id": "TC1", "tool_name": "web_search", "arguments": {"q": "x"}},
            ),
        ]
        views = Projector.fold_tool_results(evs)
        self.assertEqual(len(views), 1)
        self.assertEqual(views[0].status, "pending")
        self.assertEqual(views[0].arguments, {"q": "x"})

    def test_succeeded_view(self) -> None:
        evs = [
            _snap(
                type="agent.tool_called",
                payload={"tool_call_id": "TC1", "tool_name": "x"},
                seconds_offset=0,
            ),
            _snap(
                type="agent.tool_result",
                payload={"tool_call_id": "TC1", "result": [1, 2]},
                seconds_offset=1,
            ),
        ]
        views = Projector.fold_tool_results(evs)
        self.assertEqual(views[0].status, "succeeded")
        self.assertEqual(views[0].result, [1, 2])

    def test_failed_view(self) -> None:
        evs = [
            _snap(
                type="agent.tool_called",
                payload={"tool_call_id": "TC1", "tool_name": "x"},
                seconds_offset=0,
            ),
            _snap(
                type="agent.tool_failed",
                payload={
                    "tool_call_id": "TC1",
                    "error_kind": "timeout",
                    "error_message": "5s",
                },
                seconds_offset=1,
            ),
        ]
        views = Projector.fold_tool_results(evs)
        self.assertEqual(views[0].status, "failed")
        self.assertEqual(views[0].error_kind, "timeout")


class BuildTimelineTests(unittest.TestCase):
    def test_message_event_renders_with_sender_and_text(self) -> None:
        evs = [
            _snap(
                type="external.message.group.normal",
                payload={
                    "raw_message": "hello",
                    "segments": [{"type": "text", "data": {"text": "hello"}}],
                    "sender": {"nickname": "alice", "user_id": 222, "card": None},
                },
            ),
        ]
        items = Projector.build_timeline(evs, tool_views=[])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].kind, "message")
        self.assertIn("alice(222)", items[0].render)
        self.assertIn("hello", items[0].render)

    def test_message_uses_segments_not_raw_message(self) -> None:
        # raw_message 含 CQ 码原文，segments 是结构化；渲染应走 segments
        evs = [
            _snap(
                type="external.message.group.normal",
                payload={
                    "raw_message": "[CQ:at,qq=999]hi",
                    "segments": [
                        {"type": "at", "data": {"qq": "999"}},
                        {"type": "text", "data": {"text": "hi"}},
                    ],
                    "sender": {"nickname": "alice", "user_id": 222},
                },
            ),
        ]
        rendered = Projector.build_timeline(evs, tool_views=[])[0].render
        self.assertIn('<at user="999"/>', rendered)
        self.assertIn("hi", rendered)
        # 不能出现 CQ 码原文
        self.assertNotIn("CQ:at", rendered)

    def test_at_segment_with_known_user_includes_name(self) -> None:
        # 同一 timeline 内出现过 user_id=999 → at 段应带 name
        evs = [
            _snap(
                type="external.message.group.normal",
                payload={
                    "segments": [{"type": "text", "data": {"text": "我来了"}}],
                    "sender": {"card": "李四", "user_id": 999},
                },
                user_id=999,
                seconds_offset=0,
            ),
            _snap(
                type="external.message.group.normal",
                payload={
                    "segments": [{"type": "at", "data": {"qq": "999"}}],
                    "sender": {"nickname": "张三", "user_id": 222},
                },
                user_id=222,
                seconds_offset=1,
            ),
        ]
        items = Projector.build_timeline(evs, tool_views=[])
        at_render = items[1].render
        self.assertIn('user="999"', at_render)
        self.assertIn('name="李四"', at_render)

    def test_at_all_segment(self) -> None:
        evs = [
            _snap(
                type="external.message.group.normal",
                payload={
                    "segments": [{"type": "at", "data": {"qq": "all"}}],
                    "sender": {"nickname": "x", "user_id": 222},
                },
            ),
        ]
        self.assertIn("<at-all/>", Projector.build_timeline(evs, tool_views=[])[0].render)

    def test_reply_segment_with_excerpt_lookup(self) -> None:
        evs = [
            _snap(
                type="external.message.group.normal",
                payload={
                    "onebot_message_id": "M-EARLIER",
                    "segments": [{"type": "text", "data": {"text": "天气怎么样"}}],
                    "sender": {"nickname": "u1", "user_id": 100},
                },
                user_id=100,
                seconds_offset=0,
            ),
            _snap(
                type="external.message.group.normal",
                payload={
                    "segments": [
                        {"type": "reply", "data": {"id": "M-EARLIER"}},
                        {"type": "text", "data": {"text": "今天有雨"}},
                    ],
                    "sender": {"nickname": "u2", "user_id": 200},
                },
                user_id=200,
                seconds_offset=1,
            ),
        ]
        items = Projector.build_timeline(evs, tool_views=[])
        rendered = items[1].render
        self.assertIn('to="M-EARLIER"', rendered)
        self.assertIn('excerpt="天气怎么样"', rendered)
        # from= 标注被引用消息的作者（u1/100），让 LLM 看清是"u2 引用 u1"，
        # 而非"u1 在发言"。
        self.assertIn('from="u1(100)"', rendered)

    def test_reply_segment_attributes_quoted_author_not_self_speaking(
        self,
    ) -> None:
        # 回归：别人引用群主（3167291813）时，reply 段必须把作者标成群主，
        # 不能让 LLM 误以为是群主本人在发言而去接话。
        evs = [
            _snap(
                type="external.message.group.normal",
                payload={
                    "onebot_message_id": "M-OWNER",
                    "segments": [{"type": "text", "data": {"text": "我饿了"}}],
                    "sender": {"nickname": "群主", "user_id": 3167291813},
                },
                user_id=3167291813,
                seconds_offset=0,
            ),
            _snap(
                type="external.message.group.normal",
                payload={
                    "onebot_message_id": "M-B",
                    "segments": [
                        {"type": "reply", "data": {"id": "M-OWNER"}},
                        {"type": "text", "data": {"text": "那去吃饭啊"}},
                    ],
                    "sender": {"nickname": "路人B", "user_id": 222},
                },
                user_id=222,
                seconds_offset=1,
            ),
        ]
        rendered = Projector.build_timeline(evs, tool_views=[])[1].render
        self.assertIn('from="群主(3167291813)"', rendered)

    def test_reply_segment_without_excerpt_renders_to_only(self) -> None:
        # 被回复消息在 timeline 窗口外 → 只渲染 to，无 excerpt
        evs = [
            _snap(
                type="external.message.group.normal",
                payload={
                    "segments": [
                        {"type": "reply", "data": {"id": "M-OUTSIDE"}},
                        {"type": "text", "data": {"text": "嗯"}},
                    ],
                    "sender": {"nickname": "u", "user_id": 1},
                },
            ),
        ]
        rendered = Projector.build_timeline(evs, tool_views=[])[0].render
        self.assertIn('to="M-OUTSIDE"', rendered)
        self.assertNotIn("excerpt=", rendered)
        # 作者也查不到（被回复消息在窗口外）→ 不渲染 from=
        self.assertNotIn("from=", rendered)

    def test_image_segment_uses_file_hash(self) -> None:
        # 富化字段（file_hash/local_path/...）由 event_ingest/media.py 写在
        # segment 顶层（不在 data 内），见 EventIngest契约.md §6.1。
        evs = [
            _snap(
                type="external.message.group.normal",
                payload={
                    "segments": [
                        {
                            "type": "image",
                            "data": {},
                            "file_hash": "abc123",
                        }
                    ],
                    "sender": {"nickname": "u", "user_id": 1},
                },
            ),
        ]
        self.assertIn(
            '<image hash="abc123"/>',
            Projector.build_timeline(evs, tool_views=[])[0].render,
        )

    def test_image_segment_without_hash_falls_back(self) -> None:
        evs = [
            _snap(
                type="external.message.group.normal",
                payload={
                    "segments": [{"type": "image", "data": {}}],
                    "sender": {"nickname": "u", "user_id": 1},
                },
            ),
        ]
        self.assertIn(
            "<image/>",
            Projector.build_timeline(evs, tool_views=[])[0].render,
        )

    def test_downloaded_image_emits_image_ref(self) -> None:
        # downloaded=true + local_path 齐全 → TimelineItem.images 收一个
        # ImageRef；同一 hash 在多 segment 出现也只算一次（hash 级去重在
        # llm_planner 那一层，这里就按 segment 顺序原样收集）。
        evs = [
            _snap(
                type="external.message.group.normal",
                payload={
                    "segments": [
                        {
                            "type": "image",
                            "data": {},
                            "file_hash": "h1",
                            "local_path": "/tmp/runtime_data/media/img/h1",
                            "mime": "image/jpeg",
                            "downloaded": True,
                        },
                        {"type": "text", "data": {"text": "看图"}},
                    ],
                    "sender": {"nickname": "u", "user_id": 1},
                },
            ),
        ]
        item = Projector.build_timeline(evs, tool_views=[])[0]
        self.assertEqual(len(item.images), 1)
        ref = item.images[0]
        self.assertEqual(ref.file_hash, "h1")
        self.assertEqual(
            ref.local_path, "/tmp/runtime_data/media/img/h1"
        )
        self.assertEqual(ref.mime, "image/jpeg")
        self.assertIn('<image hash="h1"/>', item.render)

    def test_failed_download_image_skipped_from_image_refs(self) -> None:
        # downloaded=false（URL 过期 / 网络抖动）→ 仅留占位 tag，不进 images
        evs = [
            _snap(
                type="external.message.group.normal",
                payload={
                    "segments": [
                        {
                            "type": "image",
                            "data": {},
                            "file_hash": "h2",
                            "downloaded": False,
                        }
                    ],
                    "sender": {"nickname": "u", "user_id": 1},
                },
            ),
        ]
        item = Projector.build_timeline(evs, tool_views=[])[0]
        self.assertEqual(item.images, [])
        # 有 hash 即使没下载也照常 render，给 LLM 留个"曾有图"的信号
        self.assertIn('<image hash="h2"/>', item.render)

    def test_misc_segment_types(self) -> None:
        evs = [
            _snap(
                type="external.message.group.normal",
                payload={
                    "segments": [
                        {"type": "face", "data": {"id": "1"}},
                        {"type": "record", "data": {}},
                        {"type": "video", "data": {}},
                        {"type": "poke", "data": {"qq": "555"}},
                        {"type": "forward", "data": {"id": "FW-1"}},
                        {"type": "json", "data": {}},
                        {"type": "weird_new_segment", "data": {}},
                    ],
                    "sender": {"nickname": "u", "user_id": 1},
                },
            ),
        ]
        rendered = Projector.build_timeline(evs, tool_views=[])[0].render
        self.assertIn('<face id="1"/>', rendered)
        self.assertIn("<voice/>", rendered)
        self.assertIn("<video/>", rendered)
        self.assertIn('<poke target="555"/>', rendered)
        self.assertIn('<forward id="FW-1"/>', rendered)
        self.assertIn('<card type="json"/>', rendered)
        self.assertIn('<misc type="weird_new_segment"/>', rendered)

    def test_text_with_xml_metachars_is_escaped(self) -> None:
        # 用户消息里的 < > & 不能破坏外层 <message> 结构
        evs = [
            _snap(
                type="external.message.group.normal",
                payload={
                    "segments": [
                        {
                            "type": "text",
                            "data": {"text": '<script>alert("xss")</script> & 你好'},
                        }
                    ],
                    "sender": {"nickname": "u", "user_id": 1},
                },
            ),
        ]
        rendered = Projector.build_timeline(evs, tool_views=[])[0].render
        # 原文不应出现
        self.assertNotIn("<script>", rendered)
        # 应被转义
        self.assertIn("&lt;script&gt;", rendered)
        self.assertIn("&amp;", rendered)

    def test_timestamp_is_iso_with_timezone(self) -> None:
        evs = [
            _snap(
                type="external.message.group.normal",
                payload={
                    "segments": [{"type": "text", "data": {"text": "x"}}],
                    "sender": {"nickname": "u", "user_id": 1},
                },
            ),
        ]
        rendered = Projector.build_timeline(evs, tool_views=[])[0].render
        # 2026-05-26T14:30:00+08:00 这种形态
        self.assertIn("2026-05-26T14:30:00", rendered)
        self.assertIn("+08:00", rendered)

    def test_message_falls_back_to_raw_when_segments_empty(self) -> None:
        # 异常路径：mapper 没填 segments，但有 raw_message
        evs = [
            _snap(
                type="external.message.group.normal",
                payload={
                    "raw_message": "纯文字兜底",
                    "segments": [],
                    "sender": {"nickname": "u", "user_id": 1},
                },
            ),
        ]
        rendered = Projector.build_timeline(evs, tool_views=[])[0].render
        self.assertIn("纯文字兜底", rendered)

    def test_notice_event_renders_with_kind_and_subtype(self) -> None:
        evs = [
            _snap(
                type="external.notice.group_increase",
                payload={"sub_type": "approve"},
            ),
        ]
        items = Projector.build_timeline(evs, tool_views=[])
        self.assertEqual(items[0].kind, "notice")
        self.assertIn('kind="group_increase"', items[0].render)
        self.assertIn('sub_type="approve"', items[0].render)

    def test_task_events_do_not_produce_timeline_rows(self) -> None:
        evs = [
            _snap(type="agent.task_created", payload={"task_id": "T1"}),
            _snap(
                type="agent.task_state_changed",
                payload={"task_id": "T1", "to_state": "running"},
            ),
        ]
        items = Projector.build_timeline(evs, tool_views=[])
        self.assertEqual(items, [])

    def test_tool_called_with_result_renders_paired(self) -> None:
        called = _snap(
            type="agent.tool_called",
            payload={
                "tool_call_id": "TC1",
                "tool_name": "web_search",
                "arguments": {"q": "x"},
            },
        )
        result = _snap(
            type="agent.tool_result",
            payload={"tool_call_id": "TC1", "result": [1, 2]},
            seconds_offset=1,
        )
        tool_views = Projector.fold_tool_results([called, result])
        items = Projector.build_timeline([called, result], tool_views=tool_views)
        self.assertEqual(len(items), 1)  # tool_result alone produces nothing
        self.assertEqual(items[0].kind, "tool_call")
        self.assertIn('status="succeeded"', items[0].render)
        self.assertIn("[1, 2]", items[0].render)

    def test_tool_called_without_result_renders_pending(self) -> None:
        called = _snap(
            type="agent.tool_called",
            payload={"tool_call_id": "TC1", "tool_name": "x"},
        )
        tool_views = Projector.fold_tool_results([called])
        items = Projector.build_timeline([called], tool_views=tool_views)
        self.assertIn('status="pending"', items[0].render)

    def test_reply_emitted_produces_no_timeline_row(self) -> None:
        # 架构一致性：发言统一表示为 reply 工具的 <tool-call name="reply">，
        # agent.reply_emitted 本身不再渲染成独立的 <agent-reply> 行。
        evs = [
            _snap(
                type="agent.reply_emitted",
                payload={
                    "reply_id": "R1",
                    "content": [{"type": "text", "data": {"text": "hi back"}}],
                },
            ),
        ]
        items = Projector.build_timeline(evs, tool_views=[])
        self.assertEqual(items, [])

    def test_reply_is_represented_as_reply_tool_call(self) -> None:
        # reply 走和普通工具完全一样的 <tool-call> 渲染：content 在 <args> 里，
        # 不再有 <agent-reply>。
        called = _snap(
            type="agent.tool_called",
            payload={
                "tool_call_id": "TC_R",
                "tool_name": "reply",
                "arguments": {
                    "content": [{"type": "text", "data": {"text": "哼,带伞啦"}}],
                    "target": {"kind": "group", "group_id": 100},
                },
            },
            seconds_offset=0,
        )
        result = _snap(
            type="agent.tool_result",
            payload={"tool_call_id": "TC_R", "result": {"queued": True}},
            seconds_offset=1,
        )
        tool_views = Projector.fold_tool_results([called, result])
        items = Projector.build_timeline([called, result], tool_views=tool_views)
        self.assertEqual([i.kind for i in items], ["tool_call"])
        rendered = items[0].render
        self.assertIn('<tool-call name="reply"', rendered)
        self.assertIn("哼,带伞啦", rendered)
        self.assertNotIn("<agent-reply", rendered)

    def test_reply_to_bot_message_attributes_self(self) -> None:
        # 别人引用 bot 自己的发言 → reply 段 from="我(self_id)"，QQ 命中
        # bot_user_id 即知"被引用的是我自己"。
        evs = [
            _snap(
                type="agent.reply_emitted",
                payload={
                    "reply_id": "R1",
                    "content": [{"type": "text", "data": {"text": "随便你"}}],
                },
                seconds_offset=0,
            ),
            _snap(
                type="agent.reply_delivered",
                payload={
                    "reply_id": "R1",
                    "onebot_message_id": "M-BOT",
                    "self_id": "1005089717",
                },
                seconds_offset=1,
            ),
            _snap(
                type="external.message.group.normal",
                payload={
                    "onebot_message_id": "M-C",
                    "segments": [
                        {"type": "reply", "data": {"id": "M-BOT"}},
                        {"type": "text", "data": {"text": "你还嘴硬"}},
                    ],
                    "sender": {"nickname": "路人C", "user_id": 333},
                },
                user_id=333,
                seconds_offset=2,
            ),
        ]
        items = Projector.build_timeline(evs, tool_views=[])
        msg = [i for i in items if i.kind == "message"][0].render
        self.assertIn('from="我(1005089717)"', msg)

    def test_mface_dice_rps_file_markdown_segments(self) -> None:
        evs = [
            _snap(
                type="external.message.group.normal",
                payload={
                    "segments": [
                        {"type": "mface", "data": {"summary": "[羡慕]"}},
                        {"type": "dice", "data": {"result": 4}},
                        {"type": "rps", "data": {"result": 1}},
                        {"type": "file", "data": {"name": "report.pdf"}},
                        {"type": "markdown", "data": {"content": "# hi"}},
                    ],
                    "sender": {"nickname": "u", "user_id": 1},
                },
            ),
        ]
        r = Projector.build_timeline(evs, tool_views=[])[0].render
        self.assertIn('<mface summary="[羡慕]"/>', r)
        self.assertIn('<dice value="4"/>', r)
        self.assertIn('<rps value="1"/>', r)
        self.assertIn('<file name="report.pdf"/>', r)
        self.assertIn("<markdown/>", r)

    def test_decision_emitted_idle_decision_are_filtered_out(self) -> None:
        evs = [
            _snap(type="agent.decision_emitted", payload={}),
            _snap(type="agent.idle_decision", payload={"reason": "x"}),
            _snap(type="agent.reply_delivered", payload={}),
            _snap(type="agent.reply_failed", payload={}),
        ]
        items = Projector.build_timeline(evs, tool_views=[])
        self.assertEqual(items, [])

    def test_runtime_agent_visible_event_becomes_system_hint(self) -> None:
        evs = [
            _snap(
                type="runtime.budget_exceeded",
                payload={"kind": "tokens"},
                visibility="agent_visible",
            ),
        ]
        items = Projector.build_timeline(evs, tool_views=[])
        self.assertEqual(items[0].kind, "system_hint")
        self.assertIn("budget_exceeded", items[0].render)

    def test_runtime_only_event_excluded(self) -> None:
        evs = [
            _snap(
                type="runtime.tick_started",
                payload={},
                visibility="runtime_only",
            ),
        ]
        items = Projector.build_timeline(evs, tool_views=[])
        self.assertEqual(items, [])


class ProjectIntegrationTests(unittest.TestCase):
    def test_full_project_combines_active_tasks_and_pending_results_and_timeline(
        self,
    ) -> None:
        evs = [
            _snap(
                type="external.message.group.normal",
                payload={
                    "raw_message": "weather?",
                    "sender": {"nickname": "alice", "user_id": 222},
                },
                seconds_offset=0,
            ),
            _snap(type="agent.task_created", payload={"task_id": "T1"}, seconds_offset=1),
            _snap(
                type="agent.tool_called",
                payload={
                    "task_id": "T1",
                    "tool_call_id": "TC1",
                    "tool_name": "web_search",
                    "arguments": {"q": "weather"},
                },
                seconds_offset=2,
            ),
            _snap(
                type="agent.tool_result",
                payload={"tool_call_id": "TC1", "result": "sunny"},
                seconds_offset=3,
            ),
            _snap(
                type="agent.reply_emitted",
                payload={"content": [{"type": "text", "text": "sunny"}]},
                seconds_offset=4,
            ),
        ]
        context = Projector.project(
            evs,
            scope_key="group:999",
            correlation_id="c",
            tick_seq=1,
            now=BASE_TIME + timedelta(seconds=10),
        )
        # Active task should still be pending (no state_changed → done)
        self.assertEqual(len(context.active_tasks), 1)
        self.assertEqual(context.active_tasks[0].state, "pending")
        # The completed tool call is in pending_tool_results (succeeded != pending)
        self.assertEqual(len(context.pending_tool_results), 1)
        self.assertEqual(context.pending_tool_results[0].status, "succeeded")
        # Timeline: message + tool_call (task events folded; reply_emitted no
        # longer renders — 发言统一走 reply 工具的 <tool-call name="reply">)
        kinds = [it.kind for it in context.timeline]
        self.assertEqual(kinds, ["message", "tool_call"])

    def test_decision_context_identity_fields_preserved(self) -> None:
        context = Projector.project(
            [],
            scope_key="system",
            correlation_id="xyz",
            tick_seq=42,
            now=BASE_TIME,
        )
        self.assertEqual(context.scope_key, "system")
        self.assertEqual(context.correlation_id, "xyz")
        self.assertEqual(context.tick_seq, 42)
        self.assertEqual(context.now, BASE_TIME)
        self.assertEqual(context.timeline, [])
        self.assertEqual(context.active_tasks, [])
        self.assertEqual(context.pending_tool_results, [])
        # bot_user_id 默认 None；未注入时不破坏旧用例
        self.assertIsNone(context.bot_user_id)

    def test_bot_user_id_propagates_into_decision_context(self) -> None:
        """Projector.project 收到 bot_user_id 时必须透传到 DecisionContext，
        让 LLMPlanner 渲染 <agent-input bot_user_id="..."> 属性。"""
        context = Projector.project(
            [],
            scope_key="group:100",
            correlation_id="c",
            tick_seq=1,
            now=BASE_TIME,
            bot_user_id="3167291813",
        )
        self.assertEqual(context.bot_user_id, "3167291813")


class ProgressNotesTests(unittest.TestCase):
    """progress_note 折叠：跨 tick 的 task 思考笔记被聚合到 TaskView。"""

    def test_progress_notes_appended_in_event_order(self) -> None:
        evs = [
            _snap(
                type="agent.task_created",
                payload={"task_id": "T1", "description": "answer Q"},
                seconds_offset=0,
            ),
            _snap(
                type="agent.task_progress_noted",
                payload={"task_id": "T1", "note": "found ref A"},
                seconds_offset=1,
            ),
            _snap(
                type="agent.task_progress_noted",
                payload={"task_id": "T1", "note": "found ref B"},
                seconds_offset=2,
            ),
        ]
        tasks = Projector.fold_tasks(evs, scope_key="group:1")
        self.assertEqual(len(tasks), 1)
        notes = tasks[0].progress_notes
        self.assertEqual([n.note for n in notes], ["found ref A", "found ref B"])
        self.assertLess(notes[0].at, notes[1].at)

    def test_progress_notes_dropped_for_unknown_task(self) -> None:
        evs = [
            _snap(
                type="agent.task_progress_noted",
                payload={"task_id": "ghost", "note": "n"},
            ),
        ]
        tasks = Projector.fold_tasks(evs, scope_key="group:1")
        self.assertEqual(tasks, [])

    def test_progress_notes_capped_at_max_per_task(self) -> None:
        cap = Projector.MAX_PROGRESS_NOTES_PER_TASK
        notes = []
        for i in range(cap + 3):
            notes.append(
                _snap(
                    type="agent.task_progress_noted",
                    payload={"task_id": "T1", "note": f"note-{i}"},
                    seconds_offset=i + 1,
                )
            )
        evs = [
            _snap(type="agent.task_created", payload={"task_id": "T1"}, seconds_offset=0),
            *notes,
        ]
        tasks = Projector.fold_tasks(evs, scope_key="group:1")
        kept = [n.note for n in tasks[0].progress_notes]
        # 只留尾部 cap 条
        self.assertEqual(len(kept), cap)
        self.assertEqual(kept[0], f"note-{cap + 3 - cap}")  # 第一条是被裁掉之后的
        self.assertEqual(kept[-1], f"note-{cap + 2}")

    def test_triggered_by_event_id_captured(self) -> None:
        evs = [
            _snap(
                type="agent.task_created",
                payload={"task_id": "T1", "triggered_by_event_id": "MSG_999"},
            ),
        ]
        tasks = Projector.fold_tasks(evs, scope_key="group:1")
        self.assertEqual(tasks[0].triggered_by_event_id, "MSG_999")


class TimelineTrimTests(unittest.TestCase):
    """project() 应把 timeline 裁到尾部 max_timeline_items 条。"""

    def test_timeline_trimmed_to_max_when_exceeding(self) -> None:
        evs = [
            _snap(
                type="external.message.group.normal",
                payload={
                    "raw_message": f"m{i}",
                    "segments": [{"type": "text", "data": {"text": f"m{i}"}}],
                    "sender": {"nickname": "u", "user_id": 1},
                },
                event_id=f"E{i:03d}",
                seconds_offset=i,
            )
            for i in range(20)
        ]
        ctx = Projector.project(
            evs,
            scope_key="group:1",
            correlation_id="c",
            tick_seq=1,
            now=BASE_TIME,
            max_timeline_items=5,
        )
        self.assertEqual(len(ctx.timeline), 5)
        # 保留尾部
        self.assertIn("m15", ctx.timeline[0].render)
        self.assertIn("m19", ctx.timeline[-1].render)

    def test_timeline_not_trimmed_when_under_max(self) -> None:
        evs = [
            _snap(
                type="external.message.group.normal",
                payload={
                    "raw_message": "hi",
                    "segments": [{"type": "text", "data": {"text": "hi"}}],
                    "sender": {"nickname": "u", "user_id": 1},
                },
            ),
        ]
        ctx = Projector.project(
            evs,
            scope_key="group:1",
            correlation_id="c",
            tick_seq=1,
            now=BASE_TIME,
            max_timeline_items=100,
        )
        self.assertEqual(len(ctx.timeline), 1)


class ToolResultTruncationTests(unittest.TestCase):
    """巨大的工具返回必须被截断 + 标记，避免一次塞爆 prompt。"""

    def test_large_result_truncated_with_marker(self) -> None:
        big = "x" * (Projector.MAX_TOOL_RESULT_CHARS + 500)
        evs = [
            _snap(
                type="agent.tool_called",
                payload={"tool_call_id": "TC1", "tool_name": "web"},
                seconds_offset=0,
            ),
            _snap(
                type="agent.tool_result",
                payload={"tool_call_id": "TC1", "result": big},
                seconds_offset=1,
            ),
        ]
        views = Projector.fold_tool_results(evs)
        items = Projector.build_timeline(evs, tool_views=views)
        render = items[0].render
        self.assertIn("<truncated/>", render)
        # 截断长度大致符合上限（含 JSON 引号、转义略有膨胀）
        self.assertLess(len(render), Projector.MAX_TOOL_RESULT_CHARS + 600)

    def test_small_result_not_truncated(self) -> None:
        evs = [
            _snap(
                type="agent.tool_called",
                payload={"tool_call_id": "TC1", "tool_name": "web"},
                seconds_offset=0,
            ),
            _snap(
                type="agent.tool_result",
                payload={"tool_call_id": "TC1", "result": {"hits": 1}},
                seconds_offset=1,
            ),
        ]
        views = Projector.fold_tool_results(evs)
        items = Projector.build_timeline(evs, tool_views=views)
        self.assertNotIn("<truncated/>", items[0].render)


class TimezoneNormalizationTests(unittest.TestCase):
    """asyncpg 读 TIMESTAMPTZ 列硬编码返回 UTC tzinfo（与 PG session timezone
    无关）；_snapshot_from_row 必须把它 normalize 回 Asia/Shanghai，否则渲染
    给 LLM 的 timeline 会出现 "+00:00" 这种和数据库写入语义（china_now() →
    +08:00）不一致的尾巴。"""

    def test_snapshot_normalizes_utc_occurred_at_to_china_time(self) -> None:
        from datetime import datetime, timezone

        from qqbot.core.time import CHINA_TIMEZONE
        from qqbot.services.agent_loop.projection import _snapshot_from_row

        # 模拟 asyncpg 给的 UTC datetime
        utc_dt = datetime(2026, 5, 28, 1, 55, 46, tzinfo=timezone.utc)

        class _FakeRow:
            event_id = "E1"
            occurred_at = utc_dt
            origin = "external"
            type = "external.message.group.normal"
            scope = "group"
            group_id = 100
            user_id = 222
            visibility = "agent_visible"
            correlation_id = "c"
            causation_id = None
            payload: dict = {}

        snap = _snapshot_from_row(_FakeRow())
        self.assertEqual(snap.occurred_at.tzinfo, CHINA_TIMEZONE)
        # UTC 01:55 → 北京 09:55
        self.assertEqual(snap.occurred_at.hour, 9)
        self.assertEqual(snap.occurred_at.minute, 55)
        # isoformat 必须带 +08:00 尾巴 —— 这是 LLM 看到的字面
        self.assertIn("+08:00", snap.occurred_at.isoformat())
        self.assertNotIn("+00:00", snap.occurred_at.isoformat())


class RecallRenderingNoteTests(unittest.TestCase):
    """撤回事件追加新事件、原消息事件保留——契约 §5.1 撤回特例。"""

    def test_recall_emits_a_notice_row_alongside_original_message(self) -> None:
        evs = [
            _snap(
                type="external.message.group.normal",
                payload={
                    "raw_message": "oops",
                    "sender": {"nickname": "a", "user_id": 1},
                },
                event_id="MSG",
                seconds_offset=0,
            ),
            _snap(
                type="external.notice.group_recall",
                payload={"onebot_message_id": "1234", "operator_id": 1},
                event_id="REC",
                seconds_offset=1,
            ),
        ]
        items = Projector.build_timeline(evs, tool_views=[])
        kinds = [it.kind for it in items]
        # 原消息没有被改写或删除；recall 单独成行
        self.assertEqual(kinds, ["message", "notice"])
        self.assertIn('kind="group_recall"', items[1].render)


if __name__ == "__main__":
    unittest.main()

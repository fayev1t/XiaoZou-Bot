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
    # 两态契约：status ∈ {processing, complete}；成败不是状态，靠 error_kind
    # 区分（None=成功）。旧 pending/succeeded/failed 三态已收敛。

    def test_processing_when_no_result_yet(self) -> None:
        evs = [
            _snap(
                type="agent.tool_called",
                payload={"tool_call_id": "TC1", "tool_name": "web_search", "arguments": {"q": "x"}},
            ),
        ]
        views = Projector.fold_tool_results(evs)
        self.assertEqual(len(views), 1)
        self.assertEqual(views[0].status, "processing")
        self.assertEqual(views[0].arguments, {"q": "x"})

    def test_complete_success_view(self) -> None:
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
        self.assertEqual(views[0].status, "complete")
        self.assertEqual(views[0].result, [1, 2])
        # 成功的判据：complete 且 error_kind 为 None
        self.assertIsNone(views[0].error_kind)

    def test_complete_failure_view(self) -> None:
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
        self.assertEqual(views[0].status, "complete")
        self.assertEqual(views[0].error_kind, "timeout")
        # 无结构化附加字段时 error_extra 为 None（不是空 dict）。
        self.assertIsNone(views[0].error_extra)

    def test_failure_without_error_kind_folds_to_unknown(self) -> None:
        # "complete + error_kind 非 None ⇒ 失败" 是渲染判据；tool_failed 缺
        # error_kind（畸形 payload）时兜底 "unknown"，不得被误判成成功。
        evs = [
            _snap(
                type="agent.tool_called",
                payload={"tool_call_id": "TC1", "tool_name": "x"},
                seconds_offset=0,
            ),
            _snap(
                type="agent.tool_failed",
                payload={"tool_call_id": "TC1", "error_message": "boom"},
                seconds_offset=1,
            ),
        ]
        views = Projector.fold_tool_results(evs)
        self.assertEqual(views[0].status, "complete")
        self.assertEqual(views[0].error_kind, "unknown")

    def test_failed_view_captures_structured_error_extra(self) -> None:
        # tool_failed.payload 顶层里 ToolOutcome.extra 平铺进来的结构化字段
        # （required_tier / actual_tier ...）必须收进 error_extra 供渲染透给 LLM；
        # 信封字段（tool_call_id / tool_name / task_id / error_*）不得泄漏进去。
        evs = [
            _snap(
                type="agent.tool_called",
                payload={
                    "tool_call_id": "TC1",
                    "tool_name": "kick",
                    "task_id": "T1",
                },
                seconds_offset=0,
            ),
            _snap(
                type="agent.tool_failed",
                payload={
                    "tool_call_id": "TC1",
                    "tool_name": "kick",
                    "task_id": "T1",
                    "error_kind": "permission_denied_user_tier",
                    "error_message": "kick requires ADMIN; user tier is GUEST",
                    "required_tier": "ADMIN",
                    "actual_tier": "GUEST",
                },
                seconds_offset=1,
            ),
        ]
        view = Projector.fold_tool_results(evs)[0]
        self.assertEqual(view.status, "complete")
        self.assertEqual(view.error_kind, "permission_denied_user_tier")
        self.assertEqual(
            view.error_extra, {"required_tier": "ADMIN", "actual_tier": "GUEST"}
        )
        for envelope_key in (
            "tool_call_id",
            "tool_name",
            "task_id",
            "error_kind",
            "error_message",
        ):
            self.assertNotIn(envelope_key, view.error_extra)


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
        # sender_name / sender_qq 是两个独立属性，不再拼 "昵称(QQ)" 复合串
        self.assertIn('sender_name="alice"', items[0].render)
        self.assertIn('sender_qq="222"', items[0].render)
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
        self.assertIn('<at qq="999"/>', rendered)
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
        self.assertIn('qq="999"', at_render)
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
        self.assertIn('to_message_id="M-EARLIER"', rendered)
        self.assertIn('excerpt="天气怎么样"', rendered)
        # from= 标注被引用消息的作者（u1/100），让 LLM 看清是"u2 引用 u1"，
        # 而非"u1 在发言"。
        self.assertIn('from_name="u1"', rendered)
        self.assertIn('from_qq="100"', rendered)
        # 外部作者不渲染 from_self（该属性只在被引消息是 bot 自己发的时出现）
        self.assertNotIn("from_self=", rendered)

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
        self.assertIn('from_name="群主"', rendered)
        self.assertIn('from_qq="3167291813"', rendered)

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
        self.assertIn('to_message_id="M-OUTSIDE"', rendered)
        self.assertNotIn("excerpt=", rendered)
        # 作者也查不到（被回复消息在窗口外）→ 三个作者属性都不渲染
        self.assertNotIn("from_name=", rendered)
        self.assertNotIn("from_qq=", rendered)
        self.assertNotIn("from_self=", rendered)

    def _reply_render_to(self, quoted_segments: list[dict]) -> str:
        """helper：构造"一条被回复消息 + 一条 reply 它的消息"，返回后者渲染。"""
        evs = [
            _snap(
                type="external.message.group.normal",
                payload={
                    "onebot_message_id": "M-RICH",
                    "segments": quoted_segments,
                    "sender": {"nickname": "u1", "user_id": 100},
                },
                user_id=100,
                seconds_offset=0,
            ),
            _snap(
                type="external.message.group.normal",
                payload={
                    "segments": [
                        {"type": "reply", "data": {"id": "M-RICH"}},
                        {"type": "text", "data": {"text": "哈哈哈"}},
                    ],
                    "sender": {"nickname": "u2", "user_id": 200},
                },
                user_id=200,
                seconds_offset=1,
            ),
        ]
        return Projector.build_timeline(evs, tool_views=[])[1].render

    def test_reply_excerpt_uses_sticker_summary_gloss(self) -> None:
        # 被回复的是表情包：excerpt 与消息体渲染同源取 image.data.summary，
        # 不再退化成 "[image]" 类型占位——回复链语义打通。
        rendered = self._reply_render_to(
            [{"type": "image", "data": {"summary": "[贴贴]", "emoji_id": "e1"}}]
        )
        self.assertIn('excerpt="[贴贴]"', rendered)

    def test_reply_excerpt_uses_card_summary_gloss(self) -> None:
        # 被回复的是 ark 卡片（B 站分享等）：excerpt 用卡片外显文案 prompt
        import json as json_mod

        ark = json_mod.dumps(
            {
                "app": "com.tencent.miniapp_01",
                "prompt": "[QQ小程序]哔哩哔哩",
                "meta": {"detail_1": {"title": "哔哩哔哩"}},
            }
        )
        rendered = self._reply_render_to(
            [{"type": "json", "data": {"data": ark}}]
        )
        self.assertIn('excerpt="[QQ小程序]哔哩哔哩"', rendered)

    def test_reply_excerpt_mixes_text_and_media_gloss(self) -> None:
        # 文本 + 无 summary 的普通图片：文本原文 + "[图片]" 语义占位
        rendered = self._reply_render_to(
            [
                {"type": "text", "data": {"text": "看这个"}},
                {"type": "image", "data": {"sub_type": 0}},
            ]
        )
        self.assertIn('excerpt="看这个[图片]"', rendered)

    def test_anonymous_message_renders_marker_and_anon_name(self) -> None:
        # 匿名群消息（OneBot 标准；napcat 不产生）：sender_name 退到匿名马甲
        # 名 + anonymous="true" 标记；flag 凭证只入库、绝不进 prompt。
        evs = [
            _snap(
                type="external.message.group.anonymous",
                payload={
                    "message_sub_type": "anonymous",
                    "anonymous": {
                        "id": 80000001,
                        "name": "匿名の马甲",
                        "flag": "F_SECRET",
                    },
                    "segments": [{"type": "text", "data": {"text": "悄悄说"}}],
                    "sender": {
                        "user_id": 80000001,
                        "nickname": None,
                        "card": None,
                    },
                },
                user_id=80000001,
            ),
        ]
        r = Projector.build_timeline(evs, tool_views=[])[0].render
        self.assertIn('sender_name="匿名の马甲"', r)
        self.assertIn('anonymous="true"', r)
        self.assertNotIn("F_SECRET", r)

    def test_sender_title_rendered_when_present(self) -> None:
        # 群专属头衔（napcat 消息事件不上报；其他 OneBot 实现可能给）
        evs = [
            _snap(
                type="external.message.group.normal",
                payload={
                    "segments": [{"type": "text", "data": {"text": "hi"}}],
                    "sender": {
                        "nickname": "u",
                        "user_id": 1,
                        "title": "镇群之宝",
                    },
                },
            ),
        ]
        r = Projector.build_timeline(evs, tool_views=[])[0].render
        self.assertIn('sender_title="镇群之宝"', r)

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

    def test_image_sticker_renders_kind_and_summary(self) -> None:
        # napcat data.sub_type=1（自定义表情/表情包）→ kind="sticker"；
        # summary 是 QQ 外显文案，下载失败时它是唯一语义兜底。
        evs = [
            _snap(
                type="external.message.group.normal",
                payload={
                    "segments": [
                        {
                            "type": "image",
                            "data": {"summary": "[动画表情]", "sub_type": 1},
                            "file_hash": "h-stk",
                        }
                    ],
                    "sender": {"nickname": "u", "user_id": 1},
                },
            ),
        ]
        r = Projector.build_timeline(evs, tool_views=[])[0].render
        self.assertIn(
            '<image kind="sticker" summary="[动画表情]" hash="h-stk"/>', r
        )

    def test_image_photo_renders_kind_photo(self) -> None:
        # napcat data.sub_type=0（普通图片）→ kind="photo"
        evs = [
            _snap(
                type="external.message.group.normal",
                payload={
                    "segments": [
                        {
                            "type": "image",
                            "data": {"sub_type": 0},
                            "file_hash": "h-pho",
                        }
                    ],
                    "sender": {"nickname": "u", "user_id": 1},
                },
            ),
        ]
        r = Projector.build_timeline(evs, tool_views=[])[0].render
        self.assertIn('<image kind="photo" hash="h-pho"/>', r)

    def test_market_sticker_image_gets_sticker_kind_without_subtype(
        self,
    ) -> None:
        # napcat 的商城表情（mface）接收侧折成 image 段：无 sub_type，但带
        # emoji_id/emoji_package_id 特征字段 → kind="sticker"，summary 是
        # 表情名。下载失败无 hash 时 summary 仍在，语义不丢。
        evs = [
            _snap(
                type="external.message.group.normal",
                payload={
                    "segments": [
                        {
                            "type": "image",
                            "data": {
                                "summary": "[赞]",
                                "emoji_id": "e-1",
                                "emoji_package_id": 231182,
                            },
                        }
                    ],
                    "sender": {"nickname": "u", "user_id": 1},
                },
            ),
        ]
        r = Projector.build_timeline(evs, tool_views=[])[0].render
        self.assertIn('<image kind="sticker" summary="[赞]"/>', r)

    def test_image_unknown_subtype_omits_kind(self) -> None:
        # sub_type 2..7（KHOT 等罕见类型）不猜——缺失=未知是属性总语义
        evs = [
            _snap(
                type="external.message.group.normal",
                payload={
                    "segments": [
                        {
                            "type": "image",
                            "data": {"sub_type": 3},
                            "file_hash": "h-x",
                        }
                    ],
                    "sender": {"nickname": "u", "user_id": 1},
                },
            ),
        ]
        r = Projector.build_timeline(evs, tool_views=[])[0].render
        self.assertIn('<image hash="h-x"/>', r)
        self.assertNotIn("kind=", r)

    def test_face_renders_name_from_napcat_raw_facetext(self) -> None:
        # napcat 在 data.raw.faceText 里给了表情释义；老版本带 "/" 前缀要去掉。
        # LLM 背不出 QQ 表情 id 表，没名字的 face id 是纯噪声。
        evs = [
            _snap(
                type="external.message.group.normal",
                payload={
                    "segments": [
                        {
                            "type": "face",
                            "data": {"id": "14", "raw": {"faceText": "/微笑"}},
                        }
                    ],
                    "sender": {"nickname": "u", "user_id": 1},
                },
            ),
        ]
        r = Projector.build_timeline(evs, tool_views=[])[0].render
        self.assertIn('<face face_id="14" name="微笑"/>', r)

    def test_json_ark_card_renders_structured_fields(self) -> None:
        # ark 卡片（B 站分享 / 小程序 / 公众号文章在 napcat 全走 json 段）：
        # app=应用标识, summary=QQ 外显文案(prompt), title/desc=meta.* 内容,
        # url=跳转链接（qqdocurl 优先）。此前渲染 <card format="json"/> 等于
        # 把"别人分享了什么"整个丢掉。
        import json as json_mod

        ark = json_mod.dumps(
            {
                "app": "com.tencent.miniapp_01",
                "prompt": "[QQ小程序]哔哩哔哩",
                "meta": {
                    "detail_1": {
                        "title": "哔哩哔哩",
                        "desc": "某个视频标题",
                        "qqdocurl": "https://b23.tv/xyz",
                    }
                },
            }
        )
        evs = [
            _snap(
                type="external.message.group.normal",
                payload={
                    "segments": [{"type": "json", "data": {"data": ark}}],
                    "sender": {"nickname": "u", "user_id": 1},
                },
            ),
        ]
        r = Projector.build_timeline(evs, tool_views=[])[0].render
        self.assertIn('app="com.tencent.miniapp_01"', r)
        self.assertIn('summary="[QQ小程序]哔哩哔哩"', r)
        self.assertIn('title="哔哩哔哩"', r)
        self.assertIn('desc="某个视频标题"', r)
        self.assertIn('url="https://b23.tv/xyz"', r)

    def test_json_ark_unparseable_falls_back_to_opaque_card(self) -> None:
        # data 不是合法 JSON / 解析不出任何字段 → 回退旧形态（type= 表示
        # 未解析的原始段格式）
        evs = [
            _snap(
                type="external.message.group.normal",
                payload={
                    "segments": [
                        {"type": "json", "data": {"data": "not-json{{{"}}
                    ],
                    "sender": {"nickname": "u", "user_id": 1},
                },
            ),
        ]
        r = Projector.build_timeline(evs, tool_views=[])[0].render
        self.assertIn('<card format="json"/>', r)

    def test_share_segment_renders_card_fields(self) -> None:
        # OneBot 标准 share 段（napcat 不产生，兼容其他实现）；content→desc
        # 与 ark 卡片属性名对齐。
        evs = [
            _snap(
                type="external.message.group.normal",
                payload={
                    "segments": [
                        {
                            "type": "share",
                            "data": {
                                "url": "https://s.example/1",
                                "title": "标题",
                                "content": "描述",
                            },
                        }
                    ],
                    "sender": {"nickname": "u", "user_id": 1},
                },
            ),
        ]
        r = Projector.build_timeline(evs, tool_views=[])[0].render
        self.assertIn(
            '<card format="share" title="标题" desc="描述"'
            ' url="https://s.example/1"/>',
            r,
        )

    def test_sender_role_rendered_for_admin_and_owner_only(self) -> None:
        # sender_role=发送者在本群的角色；member 是绝大多数不渲染，
        # 缺省语义（普通成员或未知）在 xml_format.md 写死。
        def _one(role: str | None) -> str:
            sender = {"nickname": "u", "user_id": 1}
            if role is not None:
                sender["role"] = role
            evs = [
                _snap(
                    type="external.message.group.normal",
                    payload={
                        "segments": [{"type": "text", "data": {"text": "hi"}}],
                        "sender": sender,
                    },
                ),
            ]
            return Projector.build_timeline(evs, tool_views=[])[0].render

        self.assertIn('sender_role="admin"', _one("admin"))
        self.assertIn('sender_role="owner"', _one("owner"))
        self.assertNotIn("sender_role=", _one("member"))
        self.assertNotIn("sender_role=", _one(None))

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
        self.assertIn('<face face_id="1"/>', rendered)
        self.assertIn("<voice/>", rendered)
        self.assertIn("<video/>", rendered)
        self.assertIn('<poke target_qq="555"/>', rendered)
        self.assertIn('<forward forward_id="FW-1"/>', rendered)
        self.assertIn('<card format="json"/>', rendered)
        self.assertIn('<misc segment_type="weird_new_segment"/>', rendered)

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

    def test_notice_attaches_names_resolved_from_recent_messages(self) -> None:
        # notice 的 user/operator 是裸 QQ 号；近期消息里出现过的人要补
        # user_name/operator_name，否则 LLM 得自己翻 timeline 对号入座。
        evs = [
            _snap(
                type="external.message.group.normal",
                payload={
                    "segments": [{"type": "text", "data": {"text": "在"}}],
                    "sender": {"card": "张三", "user_id": 555},
                },
                user_id=555,
                seconds_offset=0,
            ),
            _snap(
                type="external.message.group.normal",
                payload={
                    "segments": [{"type": "text", "data": {"text": "好"}}],
                    "sender": {"nickname": "管理员A", "user_id": 666},
                },
                user_id=666,
                seconds_offset=1,
            ),
            _snap(
                type="external.notice.group_ban",
                payload={"sub_type": "ban", "operator_id": 666, "duration": 600},
                user_id=555,
                seconds_offset=2,
            ),
        ]
        r = Projector.build_timeline(evs, tool_views=[])[-1].render
        self.assertIn('user_qq="555"', r)
        self.assertIn('user_name="张三"', r)
        self.assertIn('operator_qq="666"', r)
        self.assertIn('operator_name="管理员A"', r)

    def test_group_ban_notice_renders_duration_seconds(self) -> None:
        evs = [
            _snap(
                type="external.notice.group_ban",
                payload={"sub_type": "ban", "operator_id": 666, "duration": 600},
                user_id=555,
            ),
        ]
        r = Projector.build_timeline(evs, tool_views=[])[0].render
        self.assertIn('duration_seconds="600"', r)

    def test_lift_ban_notice_omits_duration(self) -> None:
        # 解禁没有时长概念（napcat 报 duration=0），不渲染该属性
        evs = [
            _snap(
                type="external.notice.group_ban",
                payload={
                    "sub_type": "lift_ban",
                    "operator_id": 666,
                    "duration": 0,
                },
                user_id=555,
            ),
        ]
        r = Projector.build_timeline(evs, tool_views=[])[0].render
        self.assertNotIn("duration_seconds=", r)

    def test_poke_notice_renders_action_and_suffix(self) -> None:
        # napcat raw_info 提炼的动作文案（mapper 落 payload.action/action_suffix，
        # 有值才落键）；缺失=普通戳一戳，不渲染属性
        evs = [
            _snap(
                type="external.notice.poke",
                payload={
                    "sender_id": 555,
                    "target_id": 666,
                    "action": "拍了拍",
                    "action_suffix": "的头",
                },
                user_id=555,
            ),
        ]
        r = Projector.build_timeline(evs, tool_views=[])[0].render
        self.assertIn('action="拍了拍"', r)
        self.assertIn('action_suffix="的头"', r)

    def test_poke_notice_omits_action_when_absent(self) -> None:
        evs = [
            _snap(
                type="external.notice.poke",
                payload={"sender_id": 555, "target_id": 666},
                user_id=555,
            ),
        ]
        r = Projector.build_timeline(evs, tool_views=[])[0].render
        self.assertNotIn("action=", r)
        self.assertNotIn("action_suffix=", r)

    def test_napcat_unknown_event_renders_as_system_hint(self) -> None:
        # EventIngest契约 §8 兜底事件：agent_visible 的 runtime.* 走
        # _render_runtime 泛化渲染，SystemAgentLoop 能看到协议外报文
        evs = [
            _snap(
                type="runtime.napcat_unknown_event",
                payload={
                    "post_type": "notice",
                    "sub_type": "profile_like",
                    "raw": {"notice_type": "notify"},
                },
                scope="system",
                group_id=None,
            ),
        ]
        items = Projector.build_timeline(evs, tool_views=[])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].kind, "system_hint")
        self.assertIn('kind="napcat_unknown_event"', items[0].render)
        self.assertIn("profile_like", items[0].render)

    def test_group_card_notice_renders_old_and_new(self) -> None:
        # new_card 空串=清空名片，与"缺失=mapper 没拿到"区分，所以空串也渲染
        evs = [
            _snap(
                type="external.notice.group_card",
                payload={"card_old": "旧名", "card_new": ""},
                user_id=555,
            ),
        ]
        r = Projector.build_timeline(evs, tool_views=[])[0].render
        self.assertIn('old_card="旧名"', r)
        self.assertIn('new_card=""', r)

    def test_group_upload_notice_renders_file_name_and_size(self) -> None:
        evs = [
            _snap(
                type="external.notice.group_upload",
                payload={
                    "file": {
                        "id": "f1",
                        "name": "月报.xlsx",
                        "size": 20480,
                        "busid": 102,
                    }
                },
                user_id=555,
            ),
        ]
        r = Projector.build_timeline(evs, tool_views=[])[0].render
        self.assertIn('file_name="月报.xlsx"', r)
        self.assertIn('file_size_bytes="20480"', r)
        # busid 是 napcat 内部路由参数，对模型零信息量，不透出
        self.assertNotIn("busid", r)

    def test_emoji_like_notice_renders_target_message_and_likes(self) -> None:
        # likes 两种表情形态：emoji_id 是 unicode codepoint（128077→👍）
        # 直接给字符；小整数是 QQ 黄豆 face id → "face:N"（与消息里
        # <face face_id=.../> 同一 id 空间）。
        evs = [
            _snap(
                type="external.notice.emoji_like",
                payload={
                    "onebot_message_id": "M77",
                    "likes": [
                        {"emoji_id": "128077", "count": 2},
                        {"emoji_id": "66", "count": 1},
                    ],
                },
                user_id=555,
            ),
        ]
        r = Projector.build_timeline(evs, tool_views=[])[0].render
        self.assertIn('message_id="M77"', r)
        self.assertIn("👍×2", r)
        self.assertIn("face:66×1", r)

    def test_essence_notice_renders_target_message_id(self) -> None:
        evs = [
            _snap(
                type="external.notice.essence",
                payload={
                    "sub_type": "add",
                    "onebot_message_id": "M88",
                    "operator_id": 666,
                },
                user_id=555,
            ),
        ]
        r = Projector.build_timeline(evs, tool_views=[])[0].render
        self.assertIn('message_id="M88"', r)

    def test_honor_notice_renders_honor_type(self) -> None:
        evs = [
            _snap(
                type="external.notice.honor",
                payload={"honor_type": "talkative"},
                user_id=555,
            ),
        ]
        r = Projector.build_timeline(evs, tool_views=[])[0].render
        self.assertIn('honor_type="talkative"', r)

    def test_group_join_request_renders_with_event_id_and_hides_flag(self) -> None:
        # 2026-07-03 拆分后唯一会实际渲染的 request：external.request.group.add
        # （scope=group 进目标群 timeline）→ <request kind="group.add" event_id=...>。
        # event_id 必须暴露（LLM 据此调 respond_to_group_join_request），flag 不
        # 暴露（napcat 凭证由工具用 event_id 反查，不经 LLM）。好友申请 / 邀请
        # 入群现为 runtime_only，投影取数层就被滤掉、不会走到渲染。
        evs = [
            _snap(
                type="external.request.group.add",
                payload={
                    "sub_type": "add",
                    "group_id": 67890,
                    "user_id": 222,
                    "comment": "想进群",
                    "flag": "FLAG_SECRET_xyz",
                },
                scope="group",
                group_id=67890,
                user_id=222,
                event_id="REQ_G1",
            ),
        ]
        items = Projector.build_timeline(evs, tool_views=[])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].kind, "request")
        render = items[0].render
        self.assertIn('kind="group.add"', render)
        self.assertIn('event_id="REQ_G1"', render)
        self.assertIn('group_id="67890"', render)
        self.assertIn('user_qq="222"', render)
        self.assertIn('comment="想进群"', render)
        self.assertNotIn("FLAG_SECRET", render)

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
        # complete + <result> = 成功完成（状态只回答"结束没有"）
        self.assertIn('status="complete"', items[0].render)
        self.assertIn("<result>", items[0].render)
        self.assertIn("[1, 2]", items[0].render)
        # tool-call 行必须带发起时刻 time= —— 曾是全 timeline 唯一无时间戳的
        # 行类型（bot 发言恰好渲染在这里，模型判断"多久前说过"只能靠行序）。
        self.assertIn('time="', items[0].render)

    def test_tool_called_without_result_renders_processing(self) -> None:
        called = _snap(
            type="agent.tool_called",
            payload={"tool_call_id": "TC1", "tool_name": "x"},
        )
        tool_views = Projector.fold_tool_results([called])
        items = Projector.build_timeline([called], tool_views=tool_views)
        self.assertIn('status="processing"', items[0].render)
        self.assertIn("<processing/>", items[0].render)

    def test_failed_tool_call_renders_error_extra_as_attributes(self) -> None:
        # 回归防护：结构化失败字段（required_tier/actual_tier）必须作为 <error>
        # 属性透给 LLM，而非只有 kind + message —— 曾经被 view/render 丢掉。
        called = _snap(
            type="agent.tool_called",
            payload={
                "tool_call_id": "TC1",
                "tool_name": "kick",
                "arguments": {"user_id": 5},
            },
            seconds_offset=0,
        )
        failed = _snap(
            type="agent.tool_failed",
            payload={
                "tool_call_id": "TC1",
                "tool_name": "kick",
                "error_kind": "permission_denied_user_tier",
                "error_message": "needs ADMIN",
                "required_tier": "ADMIN",
                "actual_tier": "GUEST",
            },
            seconds_offset=1,
        )
        tool_views = Projector.fold_tool_results([called, failed])
        items = Projector.build_timeline([called, failed], tool_views=tool_views)
        rendered = items[0].render
        # complete + <error> = 失败完成
        self.assertIn('status="complete"', rendered)
        self.assertIn("<error", rendered)
        self.assertIn('kind="permission_denied_user_tier"', rendered)
        self.assertIn('required_tier="ADMIN"', rendered)
        self.assertIn('actual_tier="GUEST"', rendered)
        self.assertIn("needs ADMIN", rendered)

    def test_tool_batch_completed_renders_system_hint_without_ulid(self) -> None:
        """批次收口标记（agent_visible）必须进 timeline —— 渲染成
        <system-hint kind="tool_batch_completed">，且内部 ULID
        （tool_batch_id）被剔除，只透 tool_count / tool_batch_size。"""
        ev = _snap(
            type="runtime.tool_batch_completed",
            visibility="agent_visible",
            payload={
                "tool_batch_id": "01JBATCHULIDNOISE0000000000",
                "tool_count": 2,
                "tool_batch_size": 2,
            },
        )
        items = Projector.build_timeline([ev], tool_views=[])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].kind, "system_hint")
        rendered = items[0].render
        self.assertIn('kind="tool_batch_completed"', rendered)
        self.assertIn("tool_count", rendered)
        self.assertIn('time="', rendered)  # system-hint 行同样带时间戳
        self.assertNotIn("01JBATCHULIDNOISE0000000000", rendered)
        self.assertNotIn("tool_batch_id", rendered)

    def test_reply_emitted_produces_no_timeline_row(self) -> None:
        # 架构一致性：发言统一表示为 send_message 工具的 <tool-call
        # name="send_message">，agent.reply_emitted 本身不再渲染成独立的
        # <agent-reply> 行。
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
        # send_message 走和普通工具完全一样的 <tool-call> 渲染：content 在
        # <args> 里，不再有 <agent-reply>。
        called = _snap(
            type="agent.tool_called",
            payload={
                "tool_call_id": "TC_R",
                "tool_name": "send_message",
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
        self.assertIn('<tool-call name="send_message"', rendered)
        self.assertIn("哼,带伞啦", rendered)
        self.assertNotIn("<agent-reply", rendered)

    def test_reply_to_bot_message_attributes_self(self) -> None:
        # 别人引用 bot 自己的发言 → reply 段 from_self="true"（服务端事实标注，
        # 不依赖 bot_user_id 在场）+ from_qq=self_id；bot 显示名未知，不渲染
        # from_name。
        # 发言同步后，bot 自己发言的 message_id + self_id 来自 send_message 工具的
        # tool_called（认出是发言）+ tool_result（result.message_id/self_id）。
        evs = [
            _snap(
                type="agent.tool_called",
                payload={
                    "tool_call_id": "TC_R",
                    "tool_name": "send_message",
                    "arguments": {
                        "content": [
                            {"type": "text", "data": {"text": "随便你"}}
                        ],
                        "target": {"kind": "group", "group_id": 999},
                    },
                },
                seconds_offset=0,
            ),
            _snap(
                type="agent.tool_result",
                payload={
                    "tool_call_id": "TC_R",
                    "result": {
                        "message_id": "M-BOT",
                        "self_id": "1005089717",
                        "sent": True,
                    },
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
        self.assertIn('from_self="true"', msg)
        self.assertIn('from_qq="1005089717"', msg)
        self.assertNotIn("from_name=", msg)  # bot 显示名未知，不渲染

    def test_reply_to_bot_message_attributes_self_legacy_reply_name(
        self,
    ) -> None:
        # 兼容：改名前落库的发言事件 tool_name 仍是旧的 "reply"（事件表
        # append-only）。_build_author_index 两个名都认，旧发言在一个 lookback
        # 窗口内仍能被标 from_self="true"。见 send_message工具黑盒设计 §12.2。
        evs = [
            _snap(
                type="agent.tool_called",
                payload={
                    "tool_call_id": "TC_OLD",
                    "tool_name": "reply",  # 改名前的旧事件
                    "arguments": {
                        "content": [
                            {"type": "text", "data": {"text": "旧发言"}}
                        ],
                        "target": {"kind": "group", "group_id": 999},
                    },
                },
                seconds_offset=0,
            ),
            _snap(
                type="agent.tool_result",
                payload={
                    "tool_call_id": "TC_OLD",
                    "result": {
                        "message_id": "M-OLD",
                        "self_id": "1005089717",
                        "sent": True,
                    },
                },
                seconds_offset=1,
            ),
            _snap(
                type="external.message.group.normal",
                payload={
                    "onebot_message_id": "M-D",
                    "segments": [
                        {"type": "reply", "data": {"id": "M-OLD"}},
                        {"type": "text", "data": {"text": "还嘴硬"}},
                    ],
                    "sender": {"nickname": "路人D", "user_id": 444},
                },
                user_id=444,
                seconds_offset=2,
            ),
        ]
        items = Projector.build_timeline(evs, tool_views=[])
        msg = [i for i in items if i.kind == "message"][0].render
        self.assertIn('from_self="true"', msg)
        self.assertIn('from_qq="1005089717"', msg)
        self.assertNotIn("from_name=", msg)  # bot 显示名未知，不渲染

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
        # markdown 段有 content 时渲染正文（napcat data.content；官方
        # 机器人消息常见），不再吞成 <markdown/>。
        self.assertIn("<markdown># hi</markdown>", r)

    def test_markdown_without_content_stays_empty_tag(self) -> None:
        evs = [
            _snap(
                type="external.message.group.normal",
                payload={
                    "segments": [{"type": "markdown", "data": {}}],
                    "sender": {"nickname": "u", "user_id": 1},
                },
            ),
        ]
        r = Projector.build_timeline(evs, tool_views=[])[0].render
        self.assertIn("<markdown/>", r)

    def test_markdown_long_content_clipped(self) -> None:
        evs = [
            _snap(
                type="external.message.group.normal",
                payload={
                    "segments": [
                        {"type": "markdown", "data": {"content": "x" * 600}}
                    ],
                    "sender": {"nickname": "u", "user_id": 1},
                },
            ),
        ]
        r = Projector.build_timeline(evs, tool_views=[])[0].render
        self.assertIn("x" * 500 + "…</markdown>", r)
        self.assertNotIn("x" * 501, r)

    def test_file_segment_renders_size_and_file_id(self) -> None:
        # napcat file 段带 file_size（字节）与 file_id（下载凭证，供未来
        # 文件类工具回填）；缺失时属性不渲染（缺失=未知）。
        evs = [
            _snap(
                type="external.message.group.normal",
                payload={
                    "segments": [
                        {
                            "type": "file",
                            "data": {
                                "file": "月报.xlsx",
                                "file_size": "20480",
                                "file_id": "UUID-42",
                            },
                        }
                    ],
                    "sender": {"nickname": "u", "user_id": 1},
                },
            ),
        ]
        r = Projector.build_timeline(evs, tool_views=[])[0].render
        self.assertIn(
            '<file name="月报.xlsx" size_bytes="20480" file_id="UUID-42"/>', r
        )

    def test_reply_lifecycle_events_are_filtered_out(self) -> None:
        # 发言已同步：reply_emitted/delivered/failed 不再产生（历史遗留事件也
        # 只 skip）；idle_decision 是纯运营事件不进 timeline。decision_emitted
        # 2026-07-06 起渲染 <my-thought> 行，但空白/缺失 reasoning 的仍消隐
        # ——本例 payload={} 无 reasoning，照旧不出行。发送结果由
        # send_message 工具的 <tool-call>（succeeded/failed）表达，没有独立行。
        evs = [
            _snap(type="agent.decision_emitted", payload={}),
            _snap(type="agent.idle_decision", payload={"reason": "x"}),
            _snap(type="agent.reply_emitted", payload={}),
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
        # 2026-07-02：DecisionContext 不再有 pending_tool_results 字段——工具
        # 结果只在 timeline 的 <tool-call status="complete"> 行呈现一次
        # （旧的双重渲染 + 无消费切割是复读诱饵）
        self.assertFalse(hasattr(context, "pending_tool_results"))
        # Timeline: message + tool_call (task events folded; reply_emitted no
        # longer renders — 发言统一走 send_message 工具的 <tool-call name="send_message">)
        kinds = [it.kind for it in context.timeline]
        self.assertEqual(kinds, ["message", "tool_call"])
        # timeline 的 tool_call 行必须携带完整结果（唯一出口）
        tool_row = context.timeline[1]
        self.assertIn('status="complete"', tool_row.render)
        self.assertIn("sunny", tool_row.render)

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
        # bot_user_id 默认 None；未注入时不破坏旧用例
        self.assertIsNone(context.bot_user_id)
        # 2026-07-06：跨拍自我记忆改为 timeline 的 <my-thought> 行，
        # last_reasoning / last_reasoning_at 字段已随 <last-reasoning> 删除
        self.assertFalse(hasattr(context, "last_reasoning"))
        self.assertFalse(hasattr(context, "last_reasoning_at"))

    def test_decisions_render_as_my_thought_rows(self) -> None:
        """思考轨迹内联（2026-07-06，待办清单#4）：decision_emitted 渲染
        <my-thought> 行（含 idle 拍）；空白 reasoning 的决策消隐；旧的
        fold_last_reasoning / <last-reasoning> 单条折叠已删除。"""
        evs = [
            _snap(
                type="agent.decision_emitted",
                payload={"reasoning": "第一拍：先观望"},
                seconds_offset=1,
            ),
            _snap(
                type="agent.decision_emitted",
                payload={"reasoning": "第二拍：小徐在贴日志，等他贴完"},
                seconds_offset=2,
            ),
            _snap(
                type="agent.decision_emitted",
                payload={"reasoning": "   "},  # 空白 → 无内容可看，消隐
                seconds_offset=3,
            ),
        ]
        context = Projector.project(
            evs,
            scope_key="group:999",
            correlation_id="c",
            tick_seq=2,
            now=BASE_TIME + timedelta(seconds=10),
        )
        kinds = [it.kind for it in context.timeline]
        self.assertEqual(kinds, ["my_thought", "my_thought"])
        self.assertIn("先观望", context.timeline[0].render)
        self.assertIn("等他贴完", context.timeline[1].render)
        self.assertIn('time="', context.timeline[1].render)
        # 单条折叠接口随 <last-reasoning> 一并删除，防复活
        self.assertFalse(hasattr(Projector, "fold_last_reasoning"))

    def test_task_closed_renders_timeline_row(self) -> None:
        """任务收束的事后记忆（2026-07-02）：done/failed 渲染 <task-closed>
        行（正文 = result_summary / 失败原因）；中间态迁移仍消隐。"""
        evs = [
            _snap(
                type="agent.task_state_changed",
                payload={
                    "task_id": "T1",
                    "from_state": "pending",
                    "to_state": "running",
                },
                seconds_offset=1,
            ),
            _snap(
                type="agent.task_state_changed",
                payload={
                    "task_id": "T1",
                    "to_state": "done",
                    "reason": "已把天气告诉小徐",
                },
                seconds_offset=2,
            ),
            _snap(
                type="agent.task_state_changed",
                payload={
                    "task_id": "T2",
                    "to_state": "failed",
                    "reason": "查不到这首歌",
                },
                seconds_offset=3,
            ),
        ]
        context = Projector.project(
            evs,
            scope_key="group:999",
            correlation_id="c",
            tick_seq=1,
            now=BASE_TIME + timedelta(seconds=10),
        )
        kinds = [it.kind for it in context.timeline]
        self.assertEqual(kinds, ["task_closed", "task_closed"])
        done_row = context.timeline[0].render
        self.assertIn('task_id="T1"', done_row)
        self.assertIn('outcome="done"', done_row)
        self.assertIn("已把天气告诉小徐", done_row)
        failed_row = context.timeline[1].render
        self.assertIn('outcome="failed"', failed_row)
        self.assertIn("查不到这首歌", failed_row)

    def test_bot_user_id_propagates_into_decision_context(self) -> None:
        """Projector.project 收到 bot_user_id 时必须透传到 DecisionContext，
        让 LLMPlanner 渲染 <agent-input bot_qq="..."> 属性。"""
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
        # 必须透出被撤回的 message_id——没有它 LLM 不知道撤的是哪条，
        # 会继续引用已撤回的内容（xml_format.md §notice 明细属性）。
        self.assertIn('message_id="1234"', items[1].render)


class ReplyFlushedProjectionTests(unittest.TestCase):
    def test_successful_reply_tool_row_folds_away_until_flushed(self) -> None:
        called = _snap(
            type="agent.tool_called",
            event_id="TC_EVENT",
            payload={
                "tool_call_id": "TC_REPLY",
                "tool_name": "reply",
                "arguments": {
                    "action": "upsert",
                    "gist": {"intent": "回答"},
                },
            },
        )
        result = _snap(
            type="agent.tool_result",
            payload={
                "tool_call_id": "TC_REPLY",
                "result": {
                    "reply_task_id": "R1",
                    "revision": 1,
                    "state": "open",
                },
            },
            seconds_offset=1,
        )
        views = Projector.fold_tool_results([called, result])
        self.assertEqual(
            Projector.build_timeline([called, result], tool_views=views), []
        )

    def test_reply_flushed_renders_exact_items_and_message_ids(self) -> None:
        flushed = _snap(
            type="runtime.reply_flushed",
            event_id="FLUSH",
            payload={
                "reply_task_id": "R1",
                "revision": 1,
                "status": "sent",
                "message_ids": [101, 102],
                "sent_messages": [
                    {
                        "index": 0,
                        "kind": "chat",
                        "content": [
                            {"type": "text", "data": {"text": "第一句"}}
                        ],
                        "status": "sent",
                        "message_id": 101,
                        "self_id": "10001",
                    },
                    {
                        "index": 1,
                        "kind": "meme",
                        "image_hash": "ab" * 32,
                        "status": "sent",
                        "message_id": 102,
                        "self_id": "10001",
                    },
                ],
            },
        )
        items = Projector.build_timeline([flushed], tool_views=[])
        self.assertEqual([item.kind for item in items], ["my_reply"])
        rendered = items[0].render
        self.assertIn('<my-reply reply_task_id="R1" status="sent"', rendered)
        self.assertIn('message_id="101"', rendered)
        self.assertIn("第一句", rendered)
        self.assertIn('message_id="102"', rendered)
        self.assertIn(f'hash="{"ab" * 32}"', rendered)

    def test_reply_to_flushed_message_is_marked_from_self(self) -> None:
        flushed = _snap(
            type="runtime.reply_flushed",
            payload={
                "reply_task_id": "R1",
                "status": "sent",
                "sent_messages": [
                    {
                        "kind": "chat",
                        "content": [
                            {"type": "text", "data": {"text": "说过的话"}}
                        ],
                        "status": "sent",
                        "message_id": "M-BOT",
                        "self_id": "10001",
                    }
                ],
            },
        )
        incoming = _snap(
            type="external.message.group.normal",
            payload={
                "onebot_message_id": "M-IN",
                "segments": [
                    {"type": "reply", "data": {"id": "M-BOT"}},
                    {"type": "text", "data": {"text": "知道了"}},
                ],
                "sender": {"nickname": "路人", "user_id": 2},
            },
            user_id=2,
            seconds_offset=1,
        )
        items = Projector.build_timeline([flushed, incoming], tool_views=[])
        message = [item for item in items if item.kind == "message"][0]
        self.assertIn('from_self="true"', message.render)
        self.assertIn('from_qq="10001"', message.render)


class SendMemeAuthorIndexTests(unittest.TestCase):
    """meme（action=send）也是"bot 发出一条消息"的工具，result 同样带
    message_id + self_id：别人引用 bot 发的表情包时，_build_author_index
    一并认它，reply 段照标 from_self="true"（表情包工具黑盒设计 §投影集成）。
    2026-07-12 合并前的旧工具名 send_meme 落在 append-only 事件表里，同
    reply 例保留兼容。"""

    def _meme_send_events(self, tool_name: str) -> list:
        return [
            _snap(
                type="agent.tool_called",
                payload={
                    "tool_call_id": "TC_MEME",
                    "tool_name": tool_name,
                    "arguments": {"action": "send", "image_hash": "ab" * 32},
                },
                seconds_offset=0,
            ),
            _snap(
                type="agent.tool_result",
                payload={
                    "tool_call_id": "TC_MEME",
                    "result": {
                        "action": "send",
                        "message_id": "M-MEME",
                        "self_id": "1005089717",
                        "file_hash": "ab" * 32,
                        "sent": True,
                    },
                },
                seconds_offset=1,
            ),
            _snap(
                type="external.message.group.normal",
                payload={
                    "onebot_message_id": "M-E",
                    "segments": [
                        {"type": "reply", "data": {"id": "M-MEME"}},
                        {"type": "text", "data": {"text": "这表情包好评"}},
                    ],
                    "sender": {"nickname": "路人E", "user_id": 555},
                },
                user_id=555,
                seconds_offset=2,
            ),
        ]

    def test_reply_to_bot_meme_attributes_self(self) -> None:
        evs = self._meme_send_events("meme")
        items = Projector.build_timeline(evs, tool_views=[])
        msg = [i for i in items if i.kind == "message"][0].render
        self.assertIn('from_self="true"', msg)
        self.assertIn('from_qq="1005089717"', msg)

    def test_legacy_send_meme_name_still_attributes_self(self) -> None:
        # 改名前一个 lookback 窗口内的旧发言不能丢 from_self 标注。
        evs = self._meme_send_events("send_meme")
        items = Projector.build_timeline(evs, tool_views=[])
        msg = [i for i in items if i.kind == "message"][0].render
        self.assertIn('from_self="true"', msg)
        self.assertIn('from_qq="1005089717"', msg)


class SavedMemesAugmentTests(unittest.IsolatedAsyncioTestCase):
    """收藏夹补全（_augment_with_saved_memes）：查全局 agent_memes（2026-
    07-06 起全 bot 共享，load_saved_memes 不带 scope 参数）挂到
    ctx.saved_memes；查询失败降级为原 ctx（绝不崩 tick）；system scope
    没有聊天面，跳过查询。"""

    def _ctx(self, scope_key: str = "group:100"):
        from qqbot.services.agent_loop.decision import DecisionContext

        return DecisionContext(
            scope_key=scope_key,
            correlation_id="CID",
            tick_seq=1,
            now=BASE_TIME,
        )

    async def test_augment_attaches_memes(self) -> None:
        from unittest.mock import AsyncMock, patch

        from qqbot.services.agent_loop.decision import MemeView

        meme = MemeView(
            file_hash="ab" * 32, description="黑猫瞪眼", saved_at=BASE_TIME
        )
        proj = Projector(session_factory=lambda: None)  # type: ignore[arg-type]
        with patch(
            "qqbot.services.agent_loop.meme_store.load_saved_memes",
            new=AsyncMock(return_value=[meme]),
        ):
            out = await proj._augment_with_saved_memes(
                self._ctx(), "group:100"
            )
        self.assertEqual(out.saved_memes, [meme])

    async def test_augment_degrades_on_store_error(self) -> None:
        from unittest.mock import AsyncMock, patch

        ctx = self._ctx()
        proj = Projector(session_factory=lambda: None)  # type: ignore[arg-type]
        with patch(
            "qqbot.services.agent_loop.meme_store.load_saved_memes",
            new=AsyncMock(side_effect=RuntimeError("db down")),
        ):
            out = await proj._augment_with_saved_memes(ctx, "group:100")
        self.assertIs(out, ctx)  # 降级：原样返回，不崩 tick
        self.assertEqual(out.saved_memes, [])

    async def test_system_scope_skips_query(self) -> None:
        from unittest.mock import AsyncMock, patch

        ctx = self._ctx(scope_key="system")
        proj = Projector(session_factory=lambda: None)  # type: ignore[arg-type]
        loader = AsyncMock(return_value=[])
        with patch(
            "qqbot.services.agent_loop.meme_store.load_saved_memes",
            new=loader,
        ):
            out = await proj._augment_with_saved_memes(ctx, "system")
        self.assertIs(out, ctx)
        loader.assert_not_awaited()  # system 没有收藏面，不查


class UnseenMessagesTests(unittest.TestCase):
    """第一拍判定（2026-07-06，待办清单#1 群聊拆句观望）。

    fold_unseen_message_ids 以窗口内最后一条 agent.decision_emitted 为
    水位线：其后到达的 external.message.* 是"没有任何一拍决策看过"的新
    消息，渲染时标 `unseen="true"`（缺失=已经历过至少一拍）。政策侧见
    group_chat_rules.md §半句话先等等。
    """

    def test_message_after_decision_is_unseen(self) -> None:
        evs = [
            _snap(
                type="agent.decision_emitted",
                payload={"reasoning": "r"},
                seconds_offset=1,
            ),
            _snap(
                type="external.message.group",
                event_id="M2",
                seconds_offset=2,
            ),
        ]
        self.assertEqual(
            Projector.fold_unseen_message_ids(evs), frozenset({"M2"})
        )

    def test_message_before_decision_is_seen(self) -> None:
        evs = [
            _snap(
                type="external.message.group",
                event_id="M1",
                seconds_offset=1,
            ),
            _snap(
                type="agent.decision_emitted",
                payload={"reasoning": "r"},
                seconds_offset=2,
            ),
        ]
        self.assertEqual(Projector.fold_unseen_message_ids(evs), frozenset())

    def test_messages_without_any_decision_all_unseen(self) -> None:
        # 窗口内从没有过决策 = 该 scope 真正意义上的第一拍
        evs = [
            _snap(
                type="external.message.group",
                event_id="M1",
                seconds_offset=1,
            ),
            _snap(
                type="external.message.group",
                event_id="M2",
                seconds_offset=2,
            ),
        ]
        self.assertEqual(
            Projector.fold_unseen_message_ids(evs), frozenset({"M1", "M2"})
        )

    def test_empty_window_has_no_unseen(self) -> None:
        self.assertEqual(Projector.fold_unseen_message_ids([]), frozenset())

    def test_non_message_events_never_unseen(self) -> None:
        # notice / request / runtime hint 不参与——观望语义只对"人还在说话"
        # 成立；空 reasoning 的决策也照样推进水位线（写没写 reasoning 不影响
        # "这拍看过消息"的事实）。
        evs = [
            _snap(
                type="agent.decision_emitted",
                payload={"reasoning": ""},
                seconds_offset=1,
            ),
            _snap(type="external.notice.poke", seconds_offset=2),
            _snap(type="external.request.group.add", seconds_offset=3),
            _snap(
                type="runtime.wait_elapsed",
                payload={"seconds": 15},
                seconds_offset=4,
            ),
        ]
        self.assertEqual(Projector.fold_unseen_message_ids(evs), frozenset())

    def test_project_renders_unseen_attr_only_on_new_messages(self) -> None:
        """端到端：决策前的消息不带 unseen，决策后的消息带 unseen="true"。"""
        evs = [
            _snap(
                type="external.message.group",
                event_id="M1",
                payload={
                    "sender": {"user_id": 222, "nickname": "小徐"},
                    "onebot_message_id": "101",
                    "segments": [
                        {"type": "text", "data": {"text": "我想问一下"}}
                    ],
                },
                seconds_offset=1,
            ),
            _snap(
                type="agent.decision_emitted",
                payload={"reasoning": "像半句，先等"},
                seconds_offset=2,
            ),
            _snap(
                type="external.message.group",
                event_id="M2",
                payload={
                    "sender": {"user_id": 222, "nickname": "小徐"},
                    "onebot_message_id": "102",
                    "segments": [
                        {"type": "text", "data": {"text": "关于装机那个事"}}
                    ],
                },
                seconds_offset=3,
            ),
        ]
        context = Projector.project(
            evs,
            scope_key="group:999",
            correlation_id="c",
            tick_seq=2,
            now=BASE_TIME + timedelta(seconds=10),
        )
        kinds = [it.kind for it in context.timeline]
        # 2026-07-06 思考轨迹内联后，决策事件本身也渲染 <my-thought> 行
        self.assertEqual(kinds, ["message", "my_thought", "message"])
        self.assertNotIn("unseen", context.timeline[0].render)
        self.assertIn('unseen="true"', context.timeline[2].render)

    def test_build_timeline_without_ids_marks_nothing(self) -> None:
        # 直调 build_timeline 不传 unseen_message_ids（旧调用/单测）时缺省
        # 空集——渲染行为与引入该属性前完全一致。
        evs = [
            _snap(
                type="external.message.group",
                event_id="M1",
                payload={
                    "segments": [{"type": "text", "data": {"text": "hi"}}]
                },
                seconds_offset=1,
            ),
        ]
        items = Projector.build_timeline(evs, tool_views=[])
        self.assertNotIn("unseen", items[0].render)


class MyThoughtTests(unittest.TestCase):
    """<my-thought> 行契约（2026-07-06，待办清单#4 思考轨迹内联）。

    decision_emitted（含 idle 拍）渲染为 timeline 的 <my-thought> 行：只保留
    最近 MAX_THOUGHT_ROWS 条、单条截 MAX_THOUGHT_CHARS 字、正文 XML 转义；
    project() 裁剪只数非思考行——思考行不挤占消息行预算。
    """

    @staticmethod
    def _decision(reasoning: str, offset: float) -> "_EventSnapshot":
        return _snap(
            type="agent.decision_emitted",
            payload={"reasoning": reasoning},
            event_id=f"D{int(offset * 1000)}",
            seconds_offset=offset,
        )

    def test_capped_at_max_thought_rows(self) -> None:
        evs = [self._decision(f"想法{i}", i) for i in range(1, 14)]  # 13 条
        items = Projector.build_timeline(evs, tool_views=[])
        self.assertEqual(len(items), Projector.MAX_THOUGHT_ROWS)
        # 保留的是最近 K 条（想法4..想法13），时间序不变
        self.assertIn("想法4", items[0].render)
        self.assertIn("想法13", items[-1].render)

    def test_truncated_at_max_chars(self) -> None:
        long_text = "长" * (Projector.MAX_THOUGHT_CHARS + 50)
        items = Projector.build_timeline(
            [self._decision(long_text, 1)], tool_views=[]
        )
        self.assertIn("…", items[0].render)
        self.assertNotIn(
            "长" * (Projector.MAX_THOUGHT_CHARS + 1), items[0].render
        )

    def test_reasoning_xml_escaped(self) -> None:
        items = Projector.build_timeline(
            [self._decision("对比 <b> & 引用", 1)], tool_views=[]
        )
        self.assertIn("&lt;b&gt;", items[0].render)
        self.assertNotIn("<b>", items[0].render)

    def test_thoughts_do_not_consume_message_budget(self) -> None:
        """project(max_timeline_items=3)：3 条消息预算照满，穿插的思考行
        顺带保留（4 行总量），而不是把最老一条消息挤出去。"""
        evs = [
            _snap(
                type="external.message.group",
                event_id=f"M{i}",
                payload={
                    "segments": [
                        {"type": "text", "data": {"text": f"m{i}"}}
                    ]
                },
                seconds_offset=i,
            )
            for i in range(1, 6)  # M1..M5
        ]
        evs.insert(4, self._decision("穿插的想法", 4.5))  # M4 与 M5 之间
        context = Projector.project(
            evs,
            scope_key="group:999",
            correlation_id="c",
            tick_seq=1,
            now=BASE_TIME + timedelta(seconds=10),
            max_timeline_items=3,
        )
        kinds = [it.kind for it in context.timeline]
        self.assertEqual(
            kinds, ["message", "message", "my_thought", "message"]
        )
        self.assertIn("m3", context.timeline[0].render)
        self.assertIn("m5", context.timeline[-1].render)


class WindowAnchorHysteresisTests(unittest.TestCase):
    """窗口锚定滞回契约（2026-07-12，前缀缓存）。

    timeline 裁剪起点与 <my-thought> 选择边界都钉在上一拍的锚（event_id）上，
    直到超出滞回带（TIMELINE_TRIM_SLACK / THOUGHT_ROWS_SLACK）才一次性前移
    ——保证连续各拍的 timeline 前缀逐字节稳定。锚由 build_context 从上一拍
    结果的首行取出、下一拍以入参喂回（project 仍是纯函数）。
    """

    @staticmethod
    def _msg(i: int) -> "_EventSnapshot":
        return _snap(
            type="external.message.group.normal",
            event_id=f"M{i:03d}",
            payload={
                "segments": [{"type": "text", "data": {"text": f"m{i}"}}],
                "sender": {"nickname": "u", "user_id": 1},
            },
            seconds_offset=i,
        )

    @staticmethod
    def _decision(i: int) -> "_EventSnapshot":
        return _snap(
            type="agent.decision_emitted",
            event_id=f"D{i:03d}",
            payload={"reasoning": f"想法{i}"},
            seconds_offset=i,
        )

    def _project(self, evs, *, max_items=5, anchor=None):
        return Projector.project(
            evs,
            scope_key="group:999",
            correlation_id="c",
            tick_seq=1,
            now=BASE_TIME + timedelta(seconds=999),
            max_timeline_items=max_items,
            timeline_anchor=anchor,
        )

    def test_anchor_pins_window_start_within_slack(self) -> None:
        """上一拍首行仍在滞回带内 → 起点钉住不动，窗口随新消息增长。"""
        evs = [self._msg(i) for i in range(20)]
        first = self._project(evs)  # 朴素裁剪：M015..M019
        anchor = first.timeline[0].event_id
        self.assertEqual(anchor, "M015")
        # 下一拍：新来 3 条消息，带锚投影
        evs2 = evs + [self._msg(i) for i in range(20, 23)]
        second = self._project(evs2, anchor=anchor)
        self.assertEqual(second.timeline[0].event_id, "M015")  # 起点未移
        self.assertEqual(len(second.timeline), 8)  # 5 + 3，窗口增长
        # 前缀稳定判据：上一拍的渲染序列是下一拍的严格前缀
        self.assertEqual(
            [it.render for it in first.timeline],
            [it.render for it in second.timeline[: len(first.timeline)]],
        )

    def test_anchor_exceeding_slack_recuts_to_max(self) -> None:
        """锚起的非思考行数超过 max + TIMELINE_TRIM_SLACK → 一次性收回尾部
        max 条并重新锚定。"""
        evs = [self._msg(i) for i in range(20)]
        anchor = self._project(evs).timeline[0].event_id  # M015
        grown = evs + [
            self._msg(i)
            for i in range(20, 20 + Projector.TIMELINE_TRIM_SLACK + 1)
        ]  # 锚起 5 + 31 条 > 5 + 30
        ctx = self._project(grown, anchor=anchor)
        self.assertEqual(len(ctx.timeline), 5)
        self.assertEqual(ctx.timeline[0].event_id, "M046")

    def test_missing_anchor_falls_back_to_naive_trim(self) -> None:
        """锚掉出取数窗（或重启丢内存态）→ 退回朴素裁剪，不崩不空。"""
        evs = [self._msg(i) for i in range(20)]
        ctx = self._project(evs, anchor="M_GONE")
        self.assertEqual(len(ctx.timeline), 5)
        self.assertEqual(ctx.timeline[0].event_id, "M015")

    def test_thought_anchor_pins_selection_within_slack(self) -> None:
        """思考选择边界钉在锚上：新增决策不再把第 K 旧的思考行挤出窗口。"""
        evs = [self._decision(i) for i in range(1, 14)]  # 13 条，朴素取 D004 起
        items = Projector.build_timeline(evs, tool_views=[])
        self.assertEqual(items[0].event_id, "D004")
        # 下一拍：新增 2 条决策，带锚选择 → D004 仍在，行数 12
        evs2 = evs + [self._decision(i) for i in range(14, 16)]
        items2 = Projector.build_timeline(
            evs2, tool_views=[], thought_anchor="D004"
        )
        self.assertEqual(items2[0].event_id, "D004")
        self.assertEqual(len(items2), 12)

    def test_thought_anchor_exceeding_slack_recuts_to_last_k(self) -> None:
        evs = [self._decision(i) for i in range(1, 21)]  # 20 条
        # 从 D001 起 20 条 > MAX_THOUGHT_ROWS + THOUGHT_ROWS_SLACK (15)
        items = Projector.build_timeline(
            evs, tool_views=[], thought_anchor="D001"
        )
        self.assertEqual(len(items), Projector.MAX_THOUGHT_ROWS)
        self.assertEqual(items[0].event_id, "D011")


if __name__ == "__main__":
    unittest.main()

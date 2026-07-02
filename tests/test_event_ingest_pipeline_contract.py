"""Contract tests for the v2 EventIngest pipeline.

Static + fake-driven. Does NOT require nonebot, asyncpg, or a live DB.

Contract sources:
- 开发文档/v2.0/EventIngest契约.md
- 开发文档/v2.0/事件系统设计.md
"""

from __future__ import annotations

import unittest
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

from qqbot.services.event_ingest import (
    EventIngest,
    MapperRegistry,
    finalize,
)
from qqbot.services.event_ingest import idempotency
from qqbot.services.event_ingest.mappers import (
    GroupMessageMapper,
    GroupRecallMapper,
    build_default_registry,
)
from qqbot.services.event_ingest.system_event import PartialSystemEvent


def _make_message_event(**overrides: Any) -> SimpleNamespace:
    defaults = dict(
        post_type="message",
        message_type="group",
        sub_type="normal",
        time=1716700000,
        self_id=10000,
        message_id=12345,
        group_id=999,
        user_id=222,
        raw_message="hello",
        message=[SimpleNamespace(type="text", data={"text": "hello"})],
        sender=SimpleNamespace(
            user_id=222, nickname="alice", card="A", role="member"
        ),
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_recall_event(**overrides: Any) -> SimpleNamespace:
    defaults = dict(
        post_type="notice",
        notice_type="group_recall",
        time=1716700050,
        self_id=10000,
        message_id=12345,
        group_id=999,
        user_id=222,
        operator_id=222,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class GroupMessageMapperContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mapper = GroupMessageMapper()

    def test_can_map_only_group_messages(self) -> None:
        self.assertTrue(self.mapper.can_map(_make_message_event()))
        self.assertFalse(
            self.mapper.can_map(_make_message_event(message_type="private"))
        )
        self.assertFalse(self.mapper.can_map(_make_message_event(post_type="notice")))

    def test_produces_external_message_group_normal(self) -> None:
        partial = self.mapper.map(_make_message_event())
        self.assertEqual(partial.origin, "external")
        self.assertEqual(partial.type, "external.message.group.normal")
        self.assertEqual(partial.scope, "group")
        self.assertEqual(partial.group_id, 999)
        self.assertEqual(partial.user_id, 222)
        self.assertEqual(partial.visibility, "agent_visible")

    def test_payload_includes_required_fields(self) -> None:
        partial = self.mapper.map(_make_message_event())
        for key in (
            "msg_hash",
            "onebot_message_id",
            "raw_message",
            "sender",
            "segments",
            "message_sub_type",
        ):
            self.assertIn(key, partial.payload)
        self.assertEqual(partial.payload["onebot_message_id"], "12345")
        self.assertEqual(partial.payload["sender"]["nickname"], "alice")
        self.assertEqual(
            partial.payload["segments"], [{"type": "text", "data": {"text": "hello"}}]
        )

    def test_anonymous_subtype_routes_to_anonymous_type(self) -> None:
        partial = self.mapper.map(_make_message_event(sub_type="anonymous"))
        self.assertEqual(partial.type, "external.message.group.anonymous")

    def test_notice_subtype_routes_to_notice_type(self) -> None:
        partial = self.mapper.map(_make_message_event(sub_type="notice"))
        self.assertEqual(partial.type, "external.message.group.notice")

    def test_idempotency_key_format(self) -> None:
        partial = self.mapper.map(_make_message_event())
        self.assertEqual(partial.idempotency_key, "10000:msg:12345")

    def test_optional_metadata_stored_when_present(self) -> None:
        # "有才上报"的元数据（napcat 扩展 real_seq/group_name、OneBot 标准
        # anonymous、sender 的 title/level/sex/age/area）有值才落键。
        event = _make_message_event(
            real_seq="7788",
            group_name="测试群",
            anonymous=SimpleNamespace(
                id=80000001, name="匿名の马甲", flag="F_SECRET"
            ),
            sender=SimpleNamespace(
                user_id=222,
                nickname="alice",
                card="A",
                role="member",
                title="大佬",
                level="100",
            ),
        )
        payload = self.mapper.map(event).payload
        self.assertEqual(payload["real_seq"], "7788")
        self.assertEqual(payload["group_name"], "测试群")
        self.assertEqual(payload["anonymous"]["id"], 80000001)
        self.assertEqual(payload["anonymous"]["name"], "匿名の马甲")
        # flag 是 set_group_anonymous_ban 凭证：随事件入库（渲染层不透出）
        self.assertEqual(payload["anonymous"]["flag"], "F_SECRET")
        self.assertEqual(payload["sender"]["title"], "大佬")
        self.assertEqual(payload["sender"]["level"], "100")

    def test_optional_metadata_absent_when_missing(self) -> None:
        # napcat 默认形态（无匿名/无头衔/无扩展序号）：键整个不出现，
        # 而不是落一堆 None。
        payload = self.mapper.map(_make_message_event()).payload
        for key in ("anonymous", "real_seq", "message_seq", "group_name"):
            self.assertNotIn(key, payload)
        for key in ("title", "level", "sex", "age", "area"):
            self.assertNotIn(key, payload["sender"])
        # 核心 4 键恒在（既有 payload 形状不变）
        for key in ("user_id", "nickname", "card", "role"):
            self.assertIn(key, payload["sender"])


class GroupRecallMapperContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mapper = GroupRecallMapper()

    def test_can_map_only_group_recall(self) -> None:
        self.assertTrue(self.mapper.can_map(_make_recall_event()))
        self.assertFalse(
            self.mapper.can_map(_make_recall_event(notice_type="group_admin"))
        )

    def test_produces_external_notice_group_recall(self) -> None:
        partial = self.mapper.map(_make_recall_event())
        self.assertEqual(partial.type, "external.notice.group_recall")
        self.assertEqual(partial.scope, "group")
        self.assertEqual(partial.visibility, "agent_visible")
        self.assertEqual(partial.payload["operator_id"], 222)
        self.assertEqual(partial.payload["onebot_message_id"], "12345")

    def test_idempotency_key_format(self) -> None:
        partial = self.mapper.map(_make_recall_event())
        self.assertEqual(
            partial.idempotency_key, "10000:recall:12345:1716700050"
        )


class MapperRegistryTests(unittest.TestCase):
    def test_exact_mapper_wins_over_fallback(self) -> None:
        class Fallback:
            post_type = "message"
            sub_type = None

            def can_map(self, event: Any) -> bool:
                return True

            def map(self, event: Any) -> PartialSystemEvent:
                raise NotImplementedError

        registry = MapperRegistry()
        registry.register(Fallback())
        registry.register(GroupMessageMapper())
        chosen = registry.find(_make_message_event())
        self.assertIsInstance(chosen, GroupMessageMapper)

    def test_returns_none_when_no_match(self) -> None:
        registry = build_default_registry()
        unknown = SimpleNamespace(post_type="meta_event", sub_type="heartbeat")
        self.assertIsNone(registry.find(unknown))


class FinalizeContractTests(unittest.TestCase):
    def test_correlation_id_is_self_event_id(self) -> None:
        partial = PartialSystemEvent(
            origin="external",
            type="external.message.group.normal",
            scope="group",
            group_id=1,
            user_id=2,
            visibility="agent_visible",
            payload={},
            raw=None,
            idempotency_key="k",
        )
        ev = finalize(
            partial,
            occurred_at=datetime(2026, 5, 26, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
        self.assertEqual(ev.correlation_id, ev.event_id)
        self.assertIsNone(ev.causation_id)
        self.assertEqual(ev.idempotency_key, "k")
        self.assertEqual(ev.scope, "group")


class IdempotencyHelpersTests(unittest.TestCase):
    def test_for_message(self) -> None:
        self.assertEqual(idempotency.for_message(1, 2), "1:msg:2")

    def test_for_notice_with_subtype(self) -> None:
        self.assertEqual(
            idempotency.for_notice(1, "group_admin", "set", 100, 9, 8),
            "1:notice:group_admin:set:100:9:8",
        )

    def test_for_notice_without_subtype(self) -> None:
        self.assertEqual(
            idempotency.for_notice(1, "group_recall", None, 100, 9, 9),
            "1:notice:group_recall:_:100:9:9",
        )

    def test_for_recall(self) -> None:
        self.assertEqual(idempotency.for_recall(1, 7, 100), "1:recall:7:100")

    def test_for_request(self) -> None:
        self.assertEqual(
            idempotency.for_request(1, "friend", "abc"), "1:request:friend:abc"
        )


class IngestPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_event_returns_unknown(self) -> None:
        registry = build_default_registry()
        ingest = EventIngest(registry, session_factory=_unused_session_factory)
        result = await ingest.ingest(SimpleNamespace(post_type="???"))
        self.assertEqual(result.status, "unknown")
        self.assertIsNone(result.event)

    async def test_ingest_group_message_inserts(self) -> None:
        recorder = _FakeSessionRecorder(rowcount=1)
        ingest = EventIngest(
            build_default_registry(), session_factory=recorder.factory
        )
        result = await ingest.ingest(_make_message_event())

        self.assertEqual(result.status, "inserted")
        self.assertIsNotNone(result.event)
        self.assertEqual(result.event.type, "external.message.group.normal")
        self.assertEqual(result.event.scope, "group")
        self.assertEqual(result.event.idempotency_key, "10000:msg:12345")
        self.assertEqual(recorder.commits, 1)
        self.assertEqual(recorder.executes, 1)

    async def test_ingest_group_message_duplicate(self) -> None:
        recorder = _FakeSessionRecorder(rowcount=0)
        ingest = EventIngest(
            build_default_registry(), session_factory=recorder.factory
        )
        result = await ingest.ingest(_make_message_event())
        self.assertEqual(result.status, "duplicate")
        self.assertIsNotNone(result.event)

    async def test_ingest_group_recall_inserts(self) -> None:
        recorder = _FakeSessionRecorder(rowcount=1)
        ingest = EventIngest(
            build_default_registry(), session_factory=recorder.factory
        )
        result = await ingest.ingest(_make_recall_event())
        self.assertEqual(result.status, "inserted")
        self.assertEqual(result.event.type, "external.notice.group_recall")


class _FakeSession:
    def __init__(self, rowcount: int, recorder: "_FakeSessionRecorder") -> None:
        self._rowcount = rowcount
        self._recorder = recorder

    async def execute(self, stmt: Any) -> Any:
        self._recorder.executes += 1
        return SimpleNamespace(rowcount=self._rowcount)

    async def commit(self) -> None:
        self._recorder.commits += 1

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None


class _FakeSessionRecorder:
    def __init__(self, rowcount: int) -> None:
        self._rowcount = rowcount
        self.executes = 0
        self.commits = 0

    def factory(self) -> _FakeSession:
        return _FakeSession(self._rowcount, self)


def _unused_session_factory() -> Any:
    raise AssertionError("session factory should not be invoked for unknown events")


if __name__ == "__main__":
    unittest.main()

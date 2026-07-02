"""Contract tests for the full v2 EventIngest mapper set.

Verifies:
- Every shipped mapper accepts the expected napcat post_type / sub_type / notice_type.
- Every shipped mapper emits the correct external.* type, scope, and visibility.
- idempotency_key follows the contract construction rules (EventIngest契约.md §4.1).
- build_default_registry() covers the full target set (事件系统设计.md §4.1).

Pure static + duck-typed fakes; no DB, no nonebot required.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any

from qqbot.services.event_ingest.mappers import (
    BotOfflineMapper,
    EmojiLikeMapper,
    EssenceMapper,
    FriendAddMapper,
    FriendRecallMapper,
    FriendRequestMapper,
    GroupAdminMapper,
    GroupBanMapper,
    GroupCardMapper,
    GroupDecreaseMapper,
    GroupIncreaseMapper,
    GroupRequestMapper,
    GroupUploadMapper,
    HonorMapper,
    InputStatusMapper,
    LifecycleMapper,
    LuckyKingMapper,
    PokeMapper,
    PrivateMessageMapper,
    build_default_registry,
)


def _ev(**fields: Any) -> SimpleNamespace:
    return SimpleNamespace(self_id=10000, time=1716700000, **fields)


class PrivateMessageMapperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mapper = PrivateMessageMapper()
        self.event = _ev(
            post_type="message",
            message_type="private",
            sub_type="friend",
            message_id=5,
            user_id=222,
            raw_message="hi",
            message=[],
            sender=SimpleNamespace(user_id=222, nickname="bob"),
        )

    def test_can_map(self) -> None:
        self.assertTrue(self.mapper.can_map(self.event))
        self.assertFalse(
            self.mapper.can_map(_ev(post_type="message", message_type="group"))
        )

    def test_partial(self) -> None:
        p = self.mapper.map(self.event)
        self.assertEqual(p.type, "external.message.private")
        self.assertEqual(p.scope, "private")
        self.assertIsNone(p.group_id)
        self.assertEqual(p.user_id, 222)
        self.assertEqual(p.visibility, "agent_visible")
        self.assertEqual(p.idempotency_key, "10000:msg:5")

    def test_sender_optional_sex_age_stored_when_present(self) -> None:
        # OneBot 标准私聊 sender 的 sex/age：有值才落键（napcat 不上报，
        # 标准实现可能给；仅入库供未来用，投影层不渲染）。
        event = _ev(
            post_type="message",
            message_type="private",
            sub_type="friend",
            message_id=6,
            user_id=222,
            raw_message="hi",
            message=[],
            sender=SimpleNamespace(
                user_id=222, nickname="bob", sex="male", age=18
            ),
        )
        p = self.mapper.map(event)
        self.assertEqual(p.payload["sender"]["sex"], "male")
        self.assertEqual(p.payload["sender"]["age"], 18)

    def test_sender_optional_fields_absent_by_default(self) -> None:
        p = self.mapper.map(self.event)
        self.assertNotIn("sex", p.payload["sender"])
        self.assertNotIn("age", p.payload["sender"])
        # 核心 2 键恒在
        self.assertIn("user_id", p.payload["sender"])
        self.assertIn("nickname", p.payload["sender"])


class GroupIncreaseMapperTests(unittest.TestCase):
    def test_partial(self) -> None:
        p = GroupIncreaseMapper().map(
            _ev(
                post_type="notice",
                notice_type="group_increase",
                sub_type="approve",
                group_id=99,
                user_id=222,
                operator_id=111,
            )
        )
        self.assertEqual(p.type, "external.notice.group_increase")
        self.assertEqual(p.scope, "group")
        self.assertEqual(p.group_id, 99)
        self.assertEqual(p.payload["sub_type"], "approve")
        self.assertEqual(p.payload["operator_id"], 111)


class GroupDecreaseMapperTests(unittest.TestCase):
    def test_partial(self) -> None:
        p = GroupDecreaseMapper().map(
            _ev(
                post_type="notice",
                notice_type="group_decrease",
                sub_type="kick",
                group_id=99,
                user_id=222,
                operator_id=111,
            )
        )
        self.assertEqual(p.type, "external.notice.group_decrease")
        self.assertEqual(p.payload["sub_type"], "kick")


class GroupAdminMapperTests(unittest.TestCase):
    def test_partial(self) -> None:
        p = GroupAdminMapper().map(
            _ev(
                post_type="notice",
                notice_type="group_admin",
                sub_type="set",
                group_id=99,
                user_id=222,
            )
        )
        self.assertEqual(p.type, "external.notice.group_admin")
        self.assertEqual(p.payload["sub_type"], "set")


class GroupBanMapperTests(unittest.TestCase):
    def test_partial(self) -> None:
        p = GroupBanMapper().map(
            _ev(
                post_type="notice",
                notice_type="group_ban",
                sub_type="ban",
                group_id=99,
                user_id=222,
                operator_id=111,
                duration=600,
            )
        )
        self.assertEqual(p.type, "external.notice.group_ban")
        self.assertEqual(p.payload["duration"], 600)


class GroupUploadMapperTests(unittest.TestCase):
    def test_partial_with_dict_file(self) -> None:
        p = GroupUploadMapper().map(
            _ev(
                post_type="notice",
                notice_type="group_upload",
                group_id=99,
                user_id=222,
                file={"id": "abc", "name": "x.zip", "size": 100, "url": "http://"},
            )
        )
        self.assertEqual(p.type, "external.notice.group_upload")
        self.assertEqual(p.payload["file"]["name"], "x.zip")
        self.assertIn("abc", p.idempotency_key)


class PokeMapperTests(unittest.TestCase):
    def test_group_scope_when_group_id_present(self) -> None:
        p = PokeMapper().map(
            _ev(
                post_type="notice",
                notice_type="notify",
                sub_type="poke",
                group_id=99,
                user_id=222,
                target_id=333,
            )
        )
        self.assertEqual(p.scope, "group")
        self.assertEqual(p.payload["target_id"], 333)

    def test_private_scope_without_group_id(self) -> None:
        p = PokeMapper().map(
            _ev(
                post_type="notice",
                notice_type="notify",
                sub_type="poke",
                group_id=None,
                user_id=222,
                target_id=333,
            )
        )
        self.assertEqual(p.scope, "private")


class LuckyKingMapperTests(unittest.TestCase):
    def test_partial(self) -> None:
        p = LuckyKingMapper().map(
            _ev(
                post_type="notice",
                notice_type="notify",
                sub_type="lucky_king",
                group_id=99,
                user_id=222,
                target_id=333,
            )
        )
        self.assertEqual(p.type, "external.notice.lucky_king")


class HonorMapperTests(unittest.TestCase):
    def test_partial(self) -> None:
        p = HonorMapper().map(
            _ev(
                post_type="notice",
                notice_type="notify",
                sub_type="honor",
                group_id=99,
                user_id=222,
                honor_type="talkative",
            )
        )
        self.assertEqual(p.type, "external.notice.honor")
        self.assertEqual(p.payload["honor_type"], "talkative")


class EssenceMapperTests(unittest.TestCase):
    def test_partial(self) -> None:
        p = EssenceMapper().map(
            _ev(
                post_type="notice",
                notice_type="essence",
                sub_type="add",
                group_id=99,
                sender_id=222,
                operator_id=111,
                message_id=77,
            )
        )
        self.assertEqual(p.type, "external.notice.essence")
        self.assertEqual(p.payload["sub_type"], "add")
        self.assertIn("essence:77", p.idempotency_key)


class EmojiLikeMapperTests(unittest.TestCase):
    def test_partial(self) -> None:
        p = EmojiLikeMapper().map(
            _ev(
                post_type="notice",
                notice_type="group_msg_emoji_like",
                group_id=99,
                user_id=222,
                message_id=77,
                likes=[{"emoji_id": "76", "count": 1}],
            )
        )
        self.assertEqual(p.type, "external.notice.emoji_like")
        self.assertEqual(p.payload["likes"][0]["emoji_id"], "76")


class GroupCardMapperTests(unittest.TestCase):
    def test_partial(self) -> None:
        p = GroupCardMapper().map(
            _ev(
                post_type="notice",
                notice_type="group_card",
                group_id=99,
                user_id=222,
                card_new="new",
                card_old="old",
            )
        )
        self.assertEqual(p.type, "external.notice.group_card")


class FriendRecallMapperTests(unittest.TestCase):
    def test_partial(self) -> None:
        p = FriendRecallMapper().map(
            _ev(
                post_type="notice",
                notice_type="friend_recall",
                user_id=222,
                message_id=88,
            )
        )
        self.assertEqual(p.type, "external.notice.friend_recall")
        self.assertEqual(p.scope, "private")
        self.assertIsNone(p.group_id)


class FriendAddMapperTests(unittest.TestCase):
    def test_partial(self) -> None:
        p = FriendAddMapper().map(
            _ev(post_type="notice", notice_type="friend_add", user_id=222)
        )
        self.assertEqual(p.type, "external.notice.friend_add")
        self.assertEqual(p.scope, "private")


class InputStatusMapperTests(unittest.TestCase):
    def test_visibility_is_runtime_only(self) -> None:
        p = InputStatusMapper().map(
            _ev(
                post_type="notice",
                notice_type="input_status",
                sub_type="typing",
                user_id=222,
            )
        )
        self.assertEqual(p.type, "external.notice.input_status")
        self.assertEqual(p.visibility, "runtime_only")
        self.assertEqual(p.scope, "private")


class BotOfflineMapperTests(unittest.TestCase):
    def test_system_scope_runtime_only(self) -> None:
        p = BotOfflineMapper().map(
            _ev(
                post_type="notice",
                notice_type="bot_offline",
                tag="qq",
                message="disconnected",
            )
        )
        self.assertEqual(p.type, "external.notice.bot_offline")
        self.assertEqual(p.scope, "system")
        self.assertEqual(p.visibility, "runtime_only")


class FriendRequestMapperTests(unittest.TestCase):
    def test_partial(self) -> None:
        p = FriendRequestMapper().map(
            _ev(
                post_type="request",
                request_type="friend",
                user_id=222,
                comment="please",
                flag="abc",
            )
        )
        self.assertEqual(p.type, "external.request.friend")
        self.assertEqual(p.scope, "system")
        self.assertEqual(p.idempotency_key, "10000:request:friend:abc")


class GroupRequestMapperTests(unittest.TestCase):
    def test_add_subtype(self) -> None:
        p = GroupRequestMapper().map(
            _ev(
                post_type="request",
                request_type="group",
                sub_type="add",
                group_id=99,
                user_id=222,
                comment="join",
                flag="xyz",
            )
        )
        self.assertEqual(p.type, "external.request.group.add")
        self.assertEqual(p.scope, "system")
        # scope=system 不暴露 group_id 在路由字段，但 payload 保留
        self.assertIsNone(p.group_id)
        self.assertEqual(p.payload["group_id"], 99)

    def test_invite_subtype(self) -> None:
        p = GroupRequestMapper().map(
            _ev(
                post_type="request",
                request_type="group",
                sub_type="invite",
                group_id=99,
                user_id=222,
                flag="zzz",
            )
        )
        self.assertEqual(p.type, "external.request.group.invite")


class LifecycleMapperTests(unittest.TestCase):
    def test_partial(self) -> None:
        p = LifecycleMapper().map(
            _ev(post_type="meta_event", meta_event_type="lifecycle", sub_type="connect")
        )
        self.assertEqual(p.type, "external.meta.lifecycle")
        self.assertEqual(p.scope, "system")
        self.assertEqual(p.visibility, "runtime_only")
        self.assertEqual(p.idempotency_key, "10000:lifecycle:connect:1716700000")


class RegistryCoverageTests(unittest.TestCase):
    """Sanity: build_default_registry() routes every contracted napcat event."""

    def _expect(self, registry: Any, event_kwargs: dict, expected_type: str) -> None:
        ev = _ev(**event_kwargs)
        mapper = registry.find(ev)
        self.assertIsNotNone(mapper, f"no mapper for {event_kwargs}")
        partial = mapper.map(ev)
        self.assertEqual(partial.type, expected_type)

    def test_every_contracted_event_has_a_mapper(self) -> None:
        registry = build_default_registry()
        cases: list[tuple[dict, str]] = [
            # message
            ({"post_type": "message", "message_type": "group", "sub_type": "normal",
              "message_id": 1, "group_id": 1, "user_id": 1, "raw_message": "",
              "message": [], "sender": None}, "external.message.group.normal"),
            ({"post_type": "message", "message_type": "private", "sub_type": "friend",
              "message_id": 1, "user_id": 1, "raw_message": "", "message": [],
              "sender": None}, "external.message.private"),
            # notice
            ({"post_type": "notice", "notice_type": "group_recall", "message_id": 1,
              "group_id": 1, "user_id": 1, "operator_id": 1},
             "external.notice.group_recall"),
            ({"post_type": "notice", "notice_type": "group_increase", "sub_type": "approve",
              "group_id": 1, "user_id": 1, "operator_id": 1},
             "external.notice.group_increase"),
            ({"post_type": "notice", "notice_type": "group_decrease", "sub_type": "leave",
              "group_id": 1, "user_id": 1, "operator_id": 1},
             "external.notice.group_decrease"),
            ({"post_type": "notice", "notice_type": "group_admin", "sub_type": "set",
              "group_id": 1, "user_id": 1}, "external.notice.group_admin"),
            ({"post_type": "notice", "notice_type": "group_ban", "sub_type": "ban",
              "group_id": 1, "user_id": 1, "operator_id": 1, "duration": 60},
             "external.notice.group_ban"),
            ({"post_type": "notice", "notice_type": "group_upload", "group_id": 1,
              "user_id": 1, "file": {"id": "a"}}, "external.notice.group_upload"),
            ({"post_type": "notice", "notice_type": "notify", "sub_type": "poke",
              "group_id": 1, "user_id": 1, "target_id": 2}, "external.notice.poke"),
            ({"post_type": "notice", "notice_type": "notify", "sub_type": "lucky_king",
              "group_id": 1, "user_id": 1, "target_id": 2}, "external.notice.lucky_king"),
            ({"post_type": "notice", "notice_type": "notify", "sub_type": "honor",
              "group_id": 1, "user_id": 1, "honor_type": "x"},
             "external.notice.honor"),
            ({"post_type": "notice", "notice_type": "essence", "sub_type": "add",
              "group_id": 1, "sender_id": 1, "operator_id": 1, "message_id": 1},
             "external.notice.essence"),
            ({"post_type": "notice", "notice_type": "group_msg_emoji_like",
              "group_id": 1, "user_id": 1, "message_id": 1, "likes": []},
             "external.notice.emoji_like"),
            ({"post_type": "notice", "notice_type": "group_card", "group_id": 1,
              "user_id": 1, "card_new": "n", "card_old": "o"},
             "external.notice.group_card"),
            ({"post_type": "notice", "notice_type": "friend_recall", "user_id": 1,
              "message_id": 1}, "external.notice.friend_recall"),
            ({"post_type": "notice", "notice_type": "friend_add", "user_id": 1},
             "external.notice.friend_add"),
            ({"post_type": "notice", "notice_type": "input_status",
              "sub_type": "typing", "user_id": 1}, "external.notice.input_status"),
            ({"post_type": "notice", "notice_type": "bot_offline", "tag": "qq",
              "message": "m"}, "external.notice.bot_offline"),
            # request
            ({"post_type": "request", "request_type": "friend", "user_id": 1,
              "comment": "c", "flag": "f"}, "external.request.friend"),
            ({"post_type": "request", "request_type": "group", "sub_type": "add",
              "group_id": 1, "user_id": 1, "flag": "f"},
             "external.request.group.add"),
            ({"post_type": "request", "request_type": "group", "sub_type": "invite",
              "group_id": 1, "user_id": 1, "flag": "f"},
             "external.request.group.invite"),
            # meta_event
            ({"post_type": "meta_event", "meta_event_type": "lifecycle",
              "sub_type": "connect"}, "external.meta.lifecycle"),
        ]
        for kwargs, expected in cases:
            with self.subTest(expected=expected):
                self._expect(registry, kwargs, expected)

    def test_heartbeat_intentionally_unmapped(self) -> None:
        registry = build_default_registry()
        ev = _ev(post_type="meta_event", meta_event_type="heartbeat", interval=5000)
        self.assertIsNone(
            registry.find(ev),
            "heartbeat must NOT be in the registry; it is handled via a "
            "bypass that writes runtime_data/napcat_heartbeat.json (EventIngest契约.md §7.1)",
        )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import asyncio
import importlib
import sys
import types
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


class _DummyHandler:
    def __init__(self, *, rule: object, priority: int, block: bool) -> None:
        self.rule = rule
        self.priority = priority
        self.block = block
        self.funcs: list[Any] = []

    def handle(self):
        def decorator(func):
            self.funcs.append(func)
            return func

        return decorator


class _DummyLogger:
    def __init__(self) -> None:
        self.records: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def info(self, *args: object, **kwargs: object) -> None:
        self.records.append(("info", args, dict(kwargs)))

    def error(self, *args: object, **kwargs: object) -> None:
        self.records.append(("error", args, dict(kwargs)))


def _install_friend_private_test_stubs() -> _DummyLogger:
    logger = _DummyLogger()

    qqbot_package = types.ModuleType("qqbot")
    setattr(qqbot_package, "__path__", [str(ROOT / "qqbot")])
    sys.modules["qqbot"] = qqbot_package

    plugins_package = types.ModuleType("qqbot.plugins")
    setattr(plugins_package, "__path__", [str(ROOT / "qqbot" / "plugins")])
    sys.modules["qqbot.plugins"] = plugins_package

    core_package = types.ModuleType("qqbot.core")
    setattr(core_package, "__path__", [str(ROOT / "qqbot" / "core")])
    sys.modules["qqbot.core"] = core_package

    logging_module = types.ModuleType("qqbot.core.logging")
    setattr(logging_module, "get_logger", lambda name: logger)
    sys.modules["qqbot.core.logging"] = logging_module

    nonebot_module = types.ModuleType("nonebot")
    setattr(
        nonebot_module,
        "on_request",
        lambda *, rule, priority, block: _DummyHandler(
            rule=rule,
            priority=priority,
            block=block,
        ),
    )
    setattr(
        nonebot_module,
        "on_message",
        lambda *, rule, priority, block: _DummyHandler(
            rule=rule,
            priority=priority,
            block=block,
        ),
    )
    sys.modules["nonebot"] = nonebot_module

    adapters_module = types.ModuleType("nonebot.adapters")

    class Event:
        pass

    setattr(adapters_module, "Event", Event)
    sys.modules["nonebot.adapters"] = adapters_module

    rule_module = types.ModuleType("nonebot.rule")

    class Rule:
        def __init__(self, checker):
            self.checkers = (checker,)

    setattr(rule_module, "Rule", Rule)
    sys.modules["nonebot.rule"] = rule_module

    onebot_v11_module = types.ModuleType("nonebot.adapters.onebot.v11")

    class Bot:
        pass

    class FriendRequestEvent(Event):
        pass

    class GroupRequestEvent(Event):
        pass

    class PrivateMessageEvent(Event):
        pass

    setattr(onebot_v11_module, "Bot", Bot)
    setattr(onebot_v11_module, "FriendRequestEvent", FriendRequestEvent)
    setattr(onebot_v11_module, "GroupRequestEvent", GroupRequestEvent)
    setattr(onebot_v11_module, "PrivateMessageEvent", PrivateMessageEvent)
    sys.modules["nonebot.adapters.onebot.v11"] = onebot_v11_module

    return logger


def _load_friend_private_module() -> tuple[Any, _DummyLogger]:
    logger = _install_friend_private_test_stubs()
    sys.modules.pop("qqbot.plugins.friend_private", None)
    module = importlib.import_module("qqbot.plugins.friend_private")
    return module, logger


friend_private, test_logger = _load_friend_private_module()


class _BotRecorder:
    def __init__(self, *, should_fail: bool = False) -> None:
        self.should_fail = should_fail
        self.calls: list[dict[str, object]] = []

    async def set_group_add_request(
        self,
        *,
        flag: str,
        sub_type: str,
        approve: bool,
    ) -> None:
        self.calls.append(
            {
                "flag": flag,
                "sub_type": sub_type,
                "approve": approve,
            }
        )
        if self.should_fail:
            raise RuntimeError("boom")


class _GroupRequestEvent(friend_private.GroupRequestEvent):
    def __init__(
        self,
        *,
        flag: str,
        sub_type: str,
        group_id: int,
        user_id: int,
        comment: str,
    ) -> None:
        self.flag = flag
        self.sub_type = sub_type
        self.group_id = group_id
        self.user_id = user_id
        self.comment = comment


class FriendPrivatePluginTests(unittest.TestCase):
    def setUp(self) -> None:
        test_logger.records.clear()

    def test_is_group_request_only_matches_group_request_event(self) -> None:
        group_event = _GroupRequestEvent(
            flag="group-flag",
            sub_type="invite",
            group_id=123,
            user_id=456,
            comment="hello",
        )
        friend_event = friend_private.FriendRequestEvent()

        self.assertTrue(friend_private._is_group_request(group_event))
        self.assertFalse(friend_private._is_group_request(friend_event))

    def test_handle_group_request_ignores_join_requests(self) -> None:
        bot = _BotRecorder()
        event = _GroupRequestEvent(
            flag="flag-1",
            sub_type="add",
            group_id=10001,
            user_id=20002,
            comment="申请入群",
        )

        asyncio.run(friend_private.handle_group_request(bot, event))

        self.assertEqual(bot.calls, [])
        self.assertEqual(test_logger.records, [])

    def test_handle_group_request_approves_invite_with_original_sub_type(self) -> None:
        bot = _BotRecorder()
        event = _GroupRequestEvent(
            flag="flag-1",
            sub_type="invite",
            group_id=10001,
            user_id=20002,
            comment="拉你进群",
        )

        asyncio.run(friend_private.handle_group_request(bot, event))

        self.assertEqual(
            bot.calls,
            [{"flag": "flag-1", "sub_type": "invite", "approve": True}],
        )
        level, args, kwargs = test_logger.records[-1]
        self.assertEqual(level, "info")
        self.assertEqual(args[0], "Group request approved")
        self.assertEqual(kwargs["extra"]["group_id"], 10001)
        self.assertEqual(kwargs["extra"]["user_id"], 20002)
        self.assertEqual(kwargs["extra"]["sub_type"], "invite")

    def test_handle_group_request_logs_error_when_approval_fails(self) -> None:
        bot = _BotRecorder(should_fail=True)
        event = _GroupRequestEvent(
            flag="flag-2",
            sub_type="invite",
            group_id=30003,
            user_id=40004,
            comment="拉你进群",
        )

        asyncio.run(friend_private.handle_group_request(bot, event))

        self.assertEqual(
            bot.calls,
            [{"flag": "flag-2", "sub_type": "invite", "approve": True}],
        )
        level, args, kwargs = test_logger.records[-1]
        self.assertEqual(level, "error")
        self.assertEqual(args[0], "Failed to approve group request: %s")
        self.assertEqual(str(args[1]), "boom")
        self.assertEqual(kwargs["extra"]["group_id"], 30003)
        self.assertEqual(kwargs["extra"]["user_id"], 40004)
        self.assertEqual(kwargs["extra"]["sub_type"], "invite")


if __name__ == "__main__":
    unittest.main()

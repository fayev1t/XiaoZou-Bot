"""Contract tests for RespondToRequestTool.

工具**永不 raise**——run() 无论成功失败都返回一个 `ToolOutcome`。故测试**无一处
assertRaises**：happy-path 用 `_ok(...)` 取 `.result`；失败路径 `outcome = await
tool.run(...)` 后断 `outcome.error_kind`。

Covers:
- friend approve  → set_friend_add_request(flag, approve, remark)
- group add reject → set_group_add_request(flag, sub_type, approve, reason)
- group invite     → sub_type=invite passed through
- non-system scope → tool_unavailable_in_scope（enforce_scope 拦下）
- missing request_event_id / approve / event not found / not a request / no flag
  / missing session_factory → invalid_arguments
- no bot registered → no_bot_available

flag 由工具用 request_event_id 反查事件 payload 得到，不经 arguments：
这里 stub 掉 _load_request 返回构造的 _EventSnapshot（不打真 DB），Bot 用 stub
注册进 bot_registry。
"""

from __future__ import annotations

import unittest
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from qqbot.services.agent_loop import bot_registry
from qqbot.services.agent_loop.projection import _EventSnapshot
from qqbot.services.agent_loop.tools.respond_to_request import (
    RespondToRequestTool,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
BASE_TIME = datetime(2026, 5, 26, 14, 30, 0, tzinfo=SHANGHAI)


async def _ok(tool: RespondToRequestTool, args: dict, **ctx: Any) -> dict:
    """run() 现返回 ToolOutcome；happy-path 取 .result 复用既有断言。"""
    outcome = await tool.run(args, **ctx)
    assert outcome.ok, outcome
    return outcome.result


def _req_snap(
    *,
    type: str,
    payload: dict,
    event_id: str = "REQ1",
    user_id: int | None = 12345,
) -> _EventSnapshot:
    return _EventSnapshot(
        event_id=event_id,
        occurred_at=BASE_TIME,
        origin="external",
        type=type,
        scope="system",
        group_id=None,
        user_id=user_id,
        visibility="agent_visible",
        correlation_id=None,
        causation_id=None,
        payload=payload,
    )


class _StubBot:
    def __init__(self, self_id: str = "10001") -> None:
        self.self_id = self_id
        self.calls: list[tuple[str, dict]] = []

    async def set_friend_add_request(self, **kwargs: Any) -> dict:
        self.calls.append(("set_friend_add_request", kwargs))
        return {}

    async def set_group_add_request(self, **kwargs: Any) -> dict:
        self.calls.append(("set_group_add_request", kwargs))
        return {}


def _tool_with_request(snap: _EventSnapshot | None) -> RespondToRequestTool:
    tool = RespondToRequestTool()

    async def _stub_load(event_id: str) -> _EventSnapshot | None:
        return snap

    tool._load_request = _stub_load  # type: ignore[method-assign]
    return tool


def _dummy_factory():
    def factory():
        raise AssertionError("session_factory must not be used when stubbed")

    return factory


class RespondToRequestHappyPathTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        bot_registry.clear()

    def tearDown(self) -> None:
        bot_registry.clear()

    async def test_friend_request_approve(self) -> None:
        bot = _StubBot()
        bot_registry.register(bot)
        snap = _req_snap(
            type="external.request.friend",
            payload={"user_id": 12345, "comment": "hi", "flag": "FFLAG"},
        )
        tool = _tool_with_request(snap)
        result = await _ok(
            tool,
            {"request_event_id": "REQ1", "approve": True, "remark": "老王"},
            scope_key="system",
            session_factory=_dummy_factory(),
        )
        self.assertEqual(len(bot.calls), 1)
        method, kwargs = bot.calls[0]
        self.assertEqual(method, "set_friend_add_request")
        self.assertEqual(kwargs["flag"], "FFLAG")
        self.assertTrue(kwargs["approve"])
        self.assertEqual(kwargs["remark"], "老王")
        self.assertEqual(result["request_type"], "friend")
        self.assertTrue(result["approve"])
        self.assertTrue(result["applied"])

    async def test_group_add_request_reject_with_reason(self) -> None:
        bot = _StubBot()
        bot_registry.register(bot)
        snap = _req_snap(
            type="external.request.group.add",
            payload={
                "sub_type": "add",
                "group_id": 67890,
                "user_id": 222,
                "flag": "GFLAG",
            },
        )
        tool = _tool_with_request(snap)
        result = await _ok(
            tool,
            {"request_event_id": "REQ1", "approve": False, "reason": "不约"},
            scope_key="system",
            session_factory=_dummy_factory(),
        )
        method, kwargs = bot.calls[0]
        self.assertEqual(method, "set_group_add_request")
        self.assertEqual(kwargs["flag"], "GFLAG")
        self.assertEqual(kwargs["sub_type"], "add")
        self.assertFalse(kwargs["approve"])
        self.assertEqual(kwargs["reason"], "不约")
        self.assertEqual(result["request_type"], "group")

    async def test_string_approve_false_coerced_to_reject(self) -> None:
        # 回归：approve 传字符串 "false" —— 裸 bool("false") 会误判 True → **同意**
        # 本该拒绝的申请（严重 bug）。coerce_bool 须把 "false" 稳妥转成 False。
        bot = _StubBot()
        bot_registry.register(bot)
        snap = _req_snap(
            type="external.request.friend",
            payload={"user_id": 12345, "flag": "FFLAG"},
        )
        tool = _tool_with_request(snap)
        result = await _ok(
            tool,
            {"request_event_id": "REQ1", "approve": "false"},
            scope_key="system",
            session_factory=_dummy_factory(),
        )
        method, kwargs = bot.calls[0]
        self.assertEqual(method, "set_friend_add_request")
        self.assertFalse(kwargs["approve"])  # 关键：绝不能是 True
        self.assertFalse(result["approve"])

    async def test_invalid_approve_returns_invalid_arguments(self) -> None:
        # 无法识别的 approve（"maybe"）→ invalid_arguments，先于任何 napcat 动作。
        bot = _StubBot()
        bot_registry.register(bot)
        snap = _req_snap(
            type="external.request.friend",
            payload={"user_id": 12345, "flag": "FFLAG"},
        )
        tool = _tool_with_request(snap)
        outcome = await tool.run(
            {"request_event_id": "REQ1", "approve": "maybe"},
            scope_key="system",
            session_factory=_dummy_factory(),
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "invalid_arguments")
        self.assertEqual(len(bot.calls), 0)

    async def test_group_invite_passes_sub_type_invite(self) -> None:
        bot = _StubBot()
        bot_registry.register(bot)
        snap = _req_snap(
            type="external.request.group.invite",
            payload={"sub_type": "invite", "group_id": 1, "flag": "IFLAG"},
        )
        tool = _tool_with_request(snap)
        await tool.run(
            {"request_event_id": "REQ1", "approve": True},
            scope_key="system",
            session_factory=_dummy_factory(),
        )
        _, kwargs = bot.calls[0]
        self.assertEqual(kwargs["sub_type"], "invite")


class RespondToRequestValidationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        bot_registry.clear()

    def tearDown(self) -> None:
        bot_registry.clear()

    async def test_non_system_scope_tool_unavailable(self) -> None:
        bot_registry.register(_StubBot())
        snap = _req_snap(type="external.request.friend", payload={"flag": "F"})
        tool = _tool_with_request(snap)
        outcome = await tool.run(
            {"request_event_id": "REQ1", "approve": True},
            scope_key="group:100",
            session_factory=_dummy_factory(),
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "tool_unavailable_in_scope")

    async def test_missing_request_event_id_invalid(self) -> None:
        tool = _tool_with_request(None)
        outcome = await tool.run(
            {"approve": True},
            scope_key="system",
            session_factory=_dummy_factory(),
        )
        self.assertEqual(outcome.error_kind, "invalid_arguments")

    async def test_missing_approve_invalid(self) -> None:
        tool = _tool_with_request(None)
        outcome = await tool.run(
            {"request_event_id": "REQ1"},
            scope_key="system",
            session_factory=_dummy_factory(),
        )
        self.assertEqual(outcome.error_kind, "invalid_arguments")

    async def test_event_not_found_invalid(self) -> None:
        tool = _tool_with_request(None)  # _load_request → None
        outcome = await tool.run(
            {"request_event_id": "GHOST", "approve": True},
            scope_key="system",
            session_factory=_dummy_factory(),
        )
        self.assertEqual(outcome.error_kind, "invalid_arguments")
        self.assertIn("no event found", outcome.error_message)

    async def test_event_not_a_request_invalid(self) -> None:
        snap = _req_snap(
            type="external.message.group.normal", payload={"flag": "x"}
        )
        tool = _tool_with_request(snap)
        outcome = await tool.run(
            {"request_event_id": "REQ1", "approve": True},
            scope_key="system",
            session_factory=_dummy_factory(),
        )
        self.assertEqual(outcome.error_kind, "invalid_arguments")
        self.assertIn("not a request event", outcome.error_message)

    async def test_request_without_flag_invalid(self) -> None:
        snap = _req_snap(
            type="external.request.friend", payload={"user_id": 1}
        )
        tool = _tool_with_request(snap)
        outcome = await tool.run(
            {"request_event_id": "REQ1", "approve": True},
            scope_key="system",
            session_factory=_dummy_factory(),
        )
        self.assertEqual(outcome.error_kind, "invalid_arguments")
        self.assertIn("flag", outcome.error_message)

    async def test_no_bot_available(self) -> None:
        bot_registry.clear()  # 没有任何 bot 注册
        snap = _req_snap(type="external.request.friend", payload={"flag": "F"})
        tool = _tool_with_request(snap)
        outcome = await tool.run(
            {"request_event_id": "REQ1", "approve": True},
            scope_key="system",
            session_factory=_dummy_factory(),
        )
        self.assertEqual(outcome.error_kind, "no_bot_available")

    async def test_missing_session_factory_invalid(self) -> None:
        snap = _req_snap(type="external.request.friend", payload={"flag": "F"})
        tool = _tool_with_request(snap)
        outcome = await tool.run(
            {"request_event_id": "REQ1", "approve": True},
            scope_key="system",
        )
        self.assertEqual(outcome.error_kind, "invalid_arguments")
        self.assertIn("session_factory", outcome.error_message)


class RespondToRequestMetadataTests(unittest.TestCase):
    def test_name_and_allowed_scopes(self) -> None:
        self.assertEqual(RespondToRequestTool.name, "respond_to_request")
        self.assertEqual(RespondToRequestTool.allowed_scopes, ("system",))
        required = RespondToRequestTool.arguments_schema["required"]
        self.assertIn("request_event_id", required)
        self.assertIn("approve", required)


if __name__ == "__main__":
    unittest.main()

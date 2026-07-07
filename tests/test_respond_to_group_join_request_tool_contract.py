"""Contract tests for RespondToGroupJoinRequestTool（本群入群申请审批）。

工具**永不 raise**——run() 无论成功失败都返回 `ToolOutcome`。故测试无一处
assertRaises：happy-path 用 `_ok(...)` 取 `.result`；失败路径断 `error_kind`。

Covers:
- approve / reject(+reason) → set_group_add_request(flag, sub_type="add", ...)
- approve 传字符串 "false" 稳妥转 False（回归：裸 bool("false") 会误同意）
- 仅 group scope（system 调 → tool_unavailable_in_scope）
- 权限闸门：发起人 tier 不足 → permission_denied_user_tier；bot 非管理员 →
  permission_denied_bot_role（ADMIN + required_bot_role="admin" 与 ban/kick 同构）
- 反查校验：事件不存在 / 不是 group.add / 属于别的群（锁群）/ 无 flag
- 老数据兼容：group_id 列为空时退 payload.group_id 判归属
- 缺 session_factory / 无 bot 注册
- 元数据：name / allowed_scopes / required_permission / required_bot_role / schema

flag 由工具用 request_event_id 反查事件 payload 得到，不经 arguments：stub 掉
_load_request 返回构造的 _EventSnapshot（不打真 DB），Bot 用 stub 注册进
bot_registry。权限上下文用 triggered_by_user_tier 预置 + bot_role 快照（stub bot
无 get_group_member_info → 实时查失败回退快照，见 tool_registry._resolve_live_bot_role）。
"""

from __future__ import annotations

import unittest
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from qqbot.core.permissions import PermissionTier
from qqbot.services.agent_loop import bot_registry
from qqbot.services.agent_loop.projection import _EventSnapshot
from qqbot.services.agent_loop.tools.respond_to_group_join_request import (
    RespondToGroupJoinRequestTool,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
BASE_TIME = datetime(2026, 7, 3, 14, 30, 0, tzinfo=SHANGHAI)

# 足够通过 enforce_access 的上下文：发起人 SYSTEM_ADMIN + bot 是群主 → 恒放行。
_OK_CTX = {"triggered_by_user_tier": "SYSTEM_ADMIN", "bot_role": "owner"}


async def _ok(
    tool: RespondToGroupJoinRequestTool, args: dict, **ctx: Any
) -> dict:
    outcome = await tool.run(args, **ctx)
    assert outcome.ok, outcome
    return outcome.result


def _req_snap(
    *,
    type: str = "external.request.group.add",
    payload: dict | None = None,
    event_id: str = "REQ1",
    group_id: int | None = 100,
    user_id: int | None = 222,
) -> _EventSnapshot:
    if payload is None:
        payload = {
            "sub_type": "add",
            "group_id": 100,
            "user_id": 222,
            "comment": "想进来学习",
            "flag": "GFLAG",
        }
    return _EventSnapshot(
        event_id=event_id,
        occurred_at=BASE_TIME,
        origin="external",
        type=type,
        scope="group",
        group_id=group_id,
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

    async def set_group_add_request(self, **kwargs: Any) -> dict:
        self.calls.append(("set_group_add_request", kwargs))
        return {}


def _tool_with_request(
    snap: _EventSnapshot | None,
) -> RespondToGroupJoinRequestTool:
    tool = RespondToGroupJoinRequestTool()

    async def _stub_load(event_id: str) -> _EventSnapshot | None:
        return snap

    tool._load_request = _stub_load  # type: ignore[method-assign]
    return tool


def _dummy_factory():
    def factory():
        raise AssertionError("session_factory must not be used when stubbed")

    return factory


class RespondToGroupJoinRequestHappyPathTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        bot_registry.clear()

    def tearDown(self) -> None:
        bot_registry.clear()

    async def test_approve(self) -> None:
        bot = _StubBot()
        bot_registry.register(bot)
        tool = _tool_with_request(_req_snap())
        result = await _ok(
            tool,
            {"request_event_id": "REQ1", "approve": True},
            scope_key="group:100",
            session_factory=_dummy_factory(),
            **_OK_CTX,
        )
        self.assertEqual(len(bot.calls), 1)
        method, kwargs = bot.calls[0]
        self.assertEqual(method, "set_group_add_request")
        self.assertEqual(kwargs["flag"], "GFLAG")
        self.assertEqual(kwargs["sub_type"], "add")
        self.assertTrue(kwargs["approve"])
        self.assertEqual(result["request_event_id"], "REQ1")
        self.assertEqual(result["group_id"], 100)
        self.assertEqual(result["user_id"], 222)
        self.assertTrue(result["approve"])
        self.assertTrue(result["applied"])

    async def test_reject_with_reason(self) -> None:
        bot = _StubBot()
        bot_registry.register(bot)
        tool = _tool_with_request(_req_snap())
        result = await _ok(
            tool,
            {"request_event_id": "REQ1", "approve": False, "reason": "不约"},
            scope_key="group:100",
            session_factory=_dummy_factory(),
            **_OK_CTX,
        )
        _, kwargs = bot.calls[0]
        self.assertFalse(kwargs["approve"])
        self.assertEqual(kwargs["reason"], "不约")
        self.assertFalse(result["approve"])

    async def test_string_approve_false_coerced_to_reject(self) -> None:
        # 回归：approve 传字符串 "false" —— 裸 bool("false") 会误判 True →
        # **同意**本该拒绝的申请。coerce_bool 须稳妥转成 False。
        bot = _StubBot()
        bot_registry.register(bot)
        tool = _tool_with_request(_req_snap())
        result = await _ok(
            tool,
            {"request_event_id": "REQ1", "approve": "false"},
            scope_key="group:100",
            session_factory=_dummy_factory(),
            **_OK_CTX,
        )
        self.assertFalse(bot.calls[0][1]["approve"])  # 关键：绝不能是 True
        self.assertFalse(result["approve"])

    async def test_legacy_row_without_group_column_falls_back_to_payload(
        self,
    ) -> None:
        # 拆分前落库的 group.add 事件 group_id 列为 None（当时 scope=system），
        # 归属判定退 payload.group_id。
        bot = _StubBot()
        bot_registry.register(bot)
        tool = _tool_with_request(_req_snap(group_id=None))
        result = await _ok(
            tool,
            {"request_event_id": "REQ1", "approve": True},
            scope_key="group:100",
            session_factory=_dummy_factory(),
            **_OK_CTX,
        )
        self.assertTrue(result["applied"])


class RespondToGroupJoinRequestGateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        bot_registry.clear()

    def tearDown(self) -> None:
        bot_registry.clear()

    async def test_system_scope_tool_unavailable(self) -> None:
        bot_registry.register(_StubBot())
        tool = _tool_with_request(_req_snap())
        outcome = await tool.run(
            {"request_event_id": "REQ1", "approve": True},
            scope_key="system",
            session_factory=_dummy_factory(),
            **_OK_CTX,
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "tool_unavailable_in_scope")

    async def test_guest_trigger_denied(self) -> None:
        # 不预置 tier 且无 session_factory 可解析 → 保守 GUEST < ADMIN → 拒绝。
        bot_registry.register(_StubBot())
        tool = _tool_with_request(_req_snap())
        outcome = await tool.run(
            {"request_event_id": "REQ1", "approve": True},
            scope_key="group:100",
            bot_role="owner",
        )
        self.assertEqual(outcome.error_kind, "permission_denied_user_tier")

    async def test_bot_not_admin_denied(self) -> None:
        # stub bot 无 get_group_member_info → 实时查失败回退 bot_role 快照 member。
        bot_registry.register(_StubBot())
        tool = _tool_with_request(_req_snap())
        outcome = await tool.run(
            {"request_event_id": "REQ1", "approve": True},
            scope_key="group:100",
            session_factory=_dummy_factory(),
            triggered_by_user_tier="SYSTEM_ADMIN",
            bot_role="member",
        )
        self.assertEqual(outcome.error_kind, "permission_denied_bot_role")


class RespondToGroupJoinRequestValidationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        bot_registry.clear()

    def tearDown(self) -> None:
        bot_registry.clear()

    async def _run(
        self, snap: _EventSnapshot | None, args: dict, **extra: Any
    ) -> Any:
        tool = _tool_with_request(snap)
        ctx: dict[str, Any] = {
            "scope_key": "group:100",
            "session_factory": _dummy_factory(),
            **_OK_CTX,
        }
        ctx.update(extra)
        return await tool.run(args, **ctx)

    async def test_missing_request_event_id_invalid(self) -> None:
        bot_registry.register(_StubBot())
        outcome = await self._run(None, {"approve": True})
        self.assertEqual(outcome.error_kind, "invalid_arguments")

    async def test_missing_approve_invalid(self) -> None:
        bot_registry.register(_StubBot())
        outcome = await self._run(None, {"request_event_id": "REQ1"})
        self.assertEqual(outcome.error_kind, "invalid_arguments")

    async def test_invalid_approve_no_napcat_call(self) -> None:
        bot = _StubBot()
        bot_registry.register(bot)
        outcome = await self._run(
            _req_snap(), {"request_event_id": "REQ1", "approve": "maybe"}
        )
        self.assertEqual(outcome.error_kind, "invalid_arguments")
        self.assertEqual(len(bot.calls), 0)

    async def test_event_not_found_invalid(self) -> None:
        bot_registry.register(_StubBot())
        outcome = await self._run(
            None, {"request_event_id": "GHOST", "approve": True}
        )
        self.assertEqual(outcome.error_kind, "invalid_arguments")
        self.assertIn("no event found", outcome.error_message)

    async def test_event_wrong_type_invalid(self) -> None:
        # 好友申请的 event_id 调不动本工具（那类根本不该由群内处理）。
        bot = _StubBot()
        bot_registry.register(bot)
        outcome = await self._run(
            _req_snap(
                type="external.request.friend",
                payload={"user_id": 1, "flag": "F"},
                group_id=None,
            ),
            {"request_event_id": "REQ1", "approve": True},
        )
        self.assertEqual(outcome.error_kind, "invalid_arguments")
        self.assertIn("not a group join request", outcome.error_message)
        self.assertEqual(len(bot.calls), 0)

    async def test_cross_group_event_rejected(self) -> None:
        # 锁群：事件属于 999 群，当前 scope 是 100 群 → 拒绝（A 群管理员的
        # 授权不能批到 B 群的申请）。
        bot = _StubBot()
        bot_registry.register(bot)
        snap = _req_snap(
            group_id=999,
            payload={
                "sub_type": "add",
                "group_id": 999,
                "user_id": 222,
                "flag": "GFLAG",
            },
        )
        outcome = await self._run(
            snap, {"request_event_id": "REQ1", "approve": True}
        )
        self.assertEqual(outcome.error_kind, "invalid_arguments")
        self.assertIn("belongs to group", outcome.error_message)
        self.assertEqual(len(bot.calls), 0)

    async def test_request_without_flag_invalid(self) -> None:
        bot_registry.register(_StubBot())
        outcome = await self._run(
            _req_snap(
                payload={"sub_type": "add", "group_id": 100, "user_id": 222}
            ),
            {"request_event_id": "REQ1", "approve": True},
        )
        self.assertEqual(outcome.error_kind, "invalid_arguments")
        self.assertIn("flag", outcome.error_message)

    async def test_no_bot_available(self) -> None:
        bot_registry.clear()
        outcome = await self._run(
            _req_snap(), {"request_event_id": "REQ1", "approve": True}
        )
        self.assertEqual(outcome.error_kind, "no_bot_available")

    async def test_missing_session_factory_invalid(self) -> None:
        bot_registry.register(_StubBot())
        tool = _tool_with_request(_req_snap())
        outcome = await tool.run(
            {"request_event_id": "REQ1", "approve": True},
            scope_key="group:100",
            **_OK_CTX,
        )
        self.assertEqual(outcome.error_kind, "invalid_arguments")
        self.assertIn("session_factory", outcome.error_message)


class RespondToGroupJoinRequestMetadataTests(unittest.TestCase):
    def test_metadata(self) -> None:
        tool = RespondToGroupJoinRequestTool
        self.assertEqual(tool.name, "respond_to_group_join_request")
        self.assertEqual(tool.allowed_scopes, ("group",))
        self.assertEqual(tool.required_permission, PermissionTier.ADMIN)
        self.assertEqual(tool.required_bot_role, "admin")
        required = tool.arguments_schema["required"]
        self.assertIn("request_event_id", required)
        self.assertIn("approve", required)


if __name__ == "__main__":
    unittest.main()

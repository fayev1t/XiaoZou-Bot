"""Contract tests for AgentLoop permission gate.

覆盖 AgentLoop._apply_actions 在 CallToolAction 上的两层校验：

- require_bot_admin=True + bot_role=admin/owner → 通过，写 tool_called。
- require_bot_admin=True + bot_role=member → tool_failed(bot_role)，不写 tool_called。
- require_bot_admin=True + bot_role=None (sweep 未完成) → tool_failed(bot_role)。
- required_permission=ADMIN + 触发用户是 owner/admin → 通过。
- required_permission=ADMIN + 触发用户是 member → tool_failed(user_tier)。
- required_permission=ADMIN + SUPERUSER 即使群里 member → 通过。
- required_permission=ADMIN + 缺 triggered_by_event_id 且 task 也没 anchor → 失败。
- required_permission=ADMIN + 缺 action.triggered_by_event_id 但 task 有 anchor → fall back。
- GUEST 工具（默认）无需 triggered_by → 直接通过。
- tool_registry=None → 整个闸门跳过（旧 skeleton 测试兼容）。
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from typing import Any
from unittest import mock

from qqbot.core.permissions import PermissionTier
from qqbot.services.agent_loop import (
    AgentLoop,
    CallToolAction,
    DecisionContext,
    TaskView,
)
from qqbot.services.agent_loop.tool_registry import ToolRegistry


class _RecordingSession:
    def __init__(self, store: list[Any]) -> None:
        self._store = store

    async def execute(self, stmt: Any) -> Any:
        self._store.append(stmt)
        # 给 resolve_user_tier_from_event 用——它 await execute() 后
        # .scalar_one_or_none()。我们的测试路径会 mock resolve 函数本身，
        # 这里返回一个 dummy 以防 ungaurded 调用。
        return SimpleNamespace(
            rowcount=1, scalar_one_or_none=lambda: None
        )

    async def commit(self) -> None:
        return None

    async def __aenter__(self) -> "_RecordingSession":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None


def _factory_for(store: list[Any]):
    def f() -> _RecordingSession:
        return _RecordingSession(store)

    return f


def _payloads_by_type(captured: list[Any]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for stmt in captured:
        params = stmt.compile().params
        t = params.get("type")
        if t is None:
            continue
        out.setdefault(t, []).append(params.get("payload") or {})
    return out


# ── Stub tools with different permission requirements ──


class _AdminTool:
    name = "kick_member"
    description = "kick"
    arguments_schema = {"type": "object"}
    required_permission = PermissionTier.ADMIN
    require_bot_admin = True

    async def run(self, arguments: dict, **_: Any) -> Any:
        return {}


class _AdminUserNoBotAdmin:
    """需要 admin 用户触发，但小奏不一定要是管理员（比如纯查询类需要鉴权但
    本身只读）。"""

    name = "audit_log"
    description = "audit"
    arguments_schema = {"type": "object"}
    required_permission = PermissionTier.ADMIN
    require_bot_admin = False

    async def run(self, arguments: dict, **_: Any) -> Any:
        return {}


class _GuestTool:
    name = "echo"
    description = "echo"
    arguments_schema = {"type": "object"}
    required_permission = PermissionTier.GUEST
    require_bot_admin = False

    async def run(self, arguments: dict, **_: Any) -> Any:
        return {}


def _ctx(*, bot_role: str | None, active_tasks: list[TaskView] | None = None) -> DecisionContext:
    from qqbot.core.time import china_now

    return DecisionContext(
        scope_key="group:100",
        correlation_id="CID",
        tick_seq=1,
        now=china_now(),
        active_tasks=active_tasks or [],
        bot_role=bot_role,  # type: ignore[arg-type]
    )


def _loop(*, registry: ToolRegistry | None, store: list[Any]) -> AgentLoop:
    return AgentLoop(
        scope_key="group:100",
        planner=mock.Mock(),  # 不会真的被调
        session_factory=_factory_for(store),
        tool_registry=registry,
    )


async def _dispatch(
    loop: AgentLoop, action: CallToolAction, context: DecisionContext
) -> None:
    await loop._apply_actions([action], "CID", "DID", context)


class RequireBotAdminGateTest(unittest.IsolatedAsyncioTestCase):
    async def test_bot_admin_passes_when_bot_role_admin(self) -> None:
        reg = ToolRegistry()
        reg.register(_AdminTool())
        store: list[Any] = []
        # mock tier resolution → ADMIN（不影响这个测试，因为 require_bot_admin
        # 的检查在 tier 检查之前；保险起见还是给个 ADMIN）
        with mock.patch(
            "qqbot.services.agent_loop.loop.resolve_user_tier_from_event",
            new=mock.AsyncMock(return_value=(PermissionTier.ADMIN, "42")),
        ):
            await _dispatch(
                _loop(registry=reg, store=store),
                CallToolAction(
                    tool_name="kick_member",
                    arguments={"target": 1},
                    triggered_by_event_id="E1",
                ),
                _ctx(bot_role="admin"),
            )
        types = _payloads_by_type(store)
        self.assertIn("agent.tool_called", types)
        self.assertNotIn("agent.tool_failed", types)

    async def test_bot_admin_blocks_when_bot_role_member(self) -> None:
        reg = ToolRegistry()
        reg.register(_AdminTool())
        store: list[Any] = []
        await _dispatch(
            _loop(registry=reg, store=store),
            CallToolAction(
                tool_name="kick_member",
                arguments={},
                triggered_by_event_id="E1",
            ),
            _ctx(bot_role="member"),
        )
        types = _payloads_by_type(store)
        self.assertNotIn("agent.tool_called", types)
        failed = types["agent.tool_failed"][0]
        self.assertEqual(failed["error_kind"], "permission_denied_bot_role")
        self.assertEqual(failed["actual_bot_role"], "member")

    async def test_bot_admin_blocks_when_bot_role_unknown(self) -> None:
        """sweep 未完成 → bot_role=None → 保守拒绝。"""
        reg = ToolRegistry()
        reg.register(_AdminTool())
        store: list[Any] = []
        await _dispatch(
            _loop(registry=reg, store=store),
            CallToolAction(
                tool_name="kick_member",
                arguments={},
                triggered_by_event_id="E1",
            ),
            _ctx(bot_role=None),
        )
        types = _payloads_by_type(store)
        self.assertNotIn("agent.tool_called", types)
        self.assertEqual(
            types["agent.tool_failed"][0]["error_kind"],
            "permission_denied_bot_role",
        )


class RequiredPermissionGateTest(unittest.IsolatedAsyncioTestCase):
    async def test_admin_user_passes(self) -> None:
        reg = ToolRegistry()
        reg.register(_AdminUserNoBotAdmin())
        store: list[Any] = []
        with mock.patch(
            "qqbot.services.agent_loop.loop.resolve_user_tier_from_event",
            new=mock.AsyncMock(return_value=(PermissionTier.ADMIN, "100")),
        ):
            await _dispatch(
                _loop(registry=reg, store=store),
                CallToolAction(
                    tool_name="audit_log",
                    arguments={},
                    triggered_by_event_id="E_admin_msg",
                ),
                _ctx(bot_role="member"),  # bot 不需要是 admin
            )
        types = _payloads_by_type(store)
        self.assertIn("agent.tool_called", types)
        called = types["agent.tool_called"][0]
        self.assertEqual(called["triggered_by_user_tier"], "ADMIN")
        self.assertEqual(called["triggered_by_user_id"], "100")
        self.assertEqual(called["triggered_by_event_id"], "E_admin_msg")

    async def test_member_user_blocked(self) -> None:
        reg = ToolRegistry()
        reg.register(_AdminUserNoBotAdmin())
        store: list[Any] = []
        with mock.patch(
            "qqbot.services.agent_loop.loop.resolve_user_tier_from_event",
            new=mock.AsyncMock(return_value=(PermissionTier.GUEST, "200")),
        ):
            await _dispatch(
                _loop(registry=reg, store=store),
                CallToolAction(
                    tool_name="audit_log",
                    arguments={},
                    triggered_by_event_id="E_member_msg",
                ),
                _ctx(bot_role="member"),
            )
        types = _payloads_by_type(store)
        self.assertNotIn("agent.tool_called", types)
        failed = types["agent.tool_failed"][0]
        self.assertEqual(failed["error_kind"], "permission_denied_user_tier")
        self.assertEqual(failed["required_tier"], "ADMIN")
        self.assertEqual(failed["actual_tier"], "GUEST")

    async def test_superuser_overrides_member_role(self) -> None:
        reg = ToolRegistry()
        reg.register(_AdminUserNoBotAdmin())
        store: list[Any] = []
        # 模拟 resolve 已经识别 SUPERUSER → SYSTEM_ADMIN
        with mock.patch(
            "qqbot.services.agent_loop.loop.resolve_user_tier_from_event",
            new=mock.AsyncMock(
                return_value=(PermissionTier.SYSTEM_ADMIN, "999")
            ),
        ):
            await _dispatch(
                _loop(registry=reg, store=store),
                CallToolAction(
                    tool_name="audit_log",
                    arguments={},
                    triggered_by_event_id="E_su_msg",
                ),
                _ctx(bot_role="member"),
            )
        types = _payloads_by_type(store)
        self.assertIn("agent.tool_called", types)
        self.assertEqual(
            types["agent.tool_called"][0]["triggered_by_user_tier"],
            "SYSTEM_ADMIN",
        )

    async def test_missing_triggered_by_treated_as_guest(self) -> None:
        """LLM 漏填 triggered_by_event_id 且 task 也没挂 anchor →
        resolve 返回 GUEST → ADMIN 工具失败。"""
        reg = ToolRegistry()
        reg.register(_AdminUserNoBotAdmin())
        store: list[Any] = []
        with mock.patch(
            "qqbot.services.agent_loop.loop.resolve_user_tier_from_event",
            new=mock.AsyncMock(return_value=(PermissionTier.GUEST, None)),
        ) as patched:
            await _dispatch(
                _loop(registry=reg, store=store),
                CallToolAction(
                    tool_name="audit_log",
                    arguments={},
                    # triggered_by_event_id 故意省略
                ),
                _ctx(bot_role="admin"),
            )
            # 闸门把 event_id=None 传给了 resolve
            patched.assert_awaited_with(
                None, session_factory=mock.ANY, superusers=mock.ANY
            )
        types = _payloads_by_type(store)
        self.assertNotIn("agent.tool_called", types)
        self.assertEqual(
            types["agent.tool_failed"][0]["error_kind"],
            "permission_denied_user_tier",
        )

    async def test_fallback_to_task_anchor_when_action_missing(self) -> None:
        """LLM 没在 call_tool 上填 triggered_by_event_id，但工具挂在的 task
        有 anchor → 用 task 的 anchor 兜底解析。"""
        reg = ToolRegistry()
        reg.register(_AdminUserNoBotAdmin())
        store: list[Any] = []
        from qqbot.core.time import china_now

        ctx = _ctx(
            bot_role="admin",
            active_tasks=[
                TaskView(
                    task_id="T1",
                    scope_key="group:100",
                    description="anchor task",
                    related_tools=["audit_log"],
                    parent_task_id=None,
                    state="running",
                    created_at=china_now(),
                    last_changed_at=china_now(),
                    last_change_reason=None,
                    pending_tool_call_ids=[],
                    triggered_by_event_id="E_anchor",
                )
            ],
        )
        with mock.patch(
            "qqbot.services.agent_loop.loop.resolve_user_tier_from_event",
            new=mock.AsyncMock(return_value=(PermissionTier.ADMIN, "42")),
        ) as patched:
            await _dispatch(
                _loop(registry=reg, store=store),
                CallToolAction(
                    tool_name="audit_log",
                    arguments={},
                    task_id="T1",
                    # 不填 triggered_by_event_id，期望 fallback 到 T1 的 anchor
                ),
                ctx,
            )
            patched.assert_awaited_with(
                "E_anchor", session_factory=mock.ANY, superusers=mock.ANY
            )
        types = _payloads_by_type(store)
        self.assertIn("agent.tool_called", types)
        self.assertEqual(
            types["agent.tool_called"][0]["triggered_by_event_id"], "E_anchor"
        )


class GuestToolBypassesGateTest(unittest.IsolatedAsyncioTestCase):
    async def test_guest_tool_skips_tier_check(self) -> None:
        """GUEST 工具应该直接通过；resolve 函数甚至不会被调（节省一次 DB 查）。"""
        reg = ToolRegistry()
        reg.register(_GuestTool())
        store: list[Any] = []
        with mock.patch(
            "qqbot.services.agent_loop.loop.resolve_user_tier_from_event",
            new=mock.AsyncMock(),
        ) as patched:
            await _dispatch(
                _loop(registry=reg, store=store),
                CallToolAction(tool_name="echo", arguments={"x": 1}),
                _ctx(bot_role="member"),
            )
            patched.assert_not_awaited()
        types = _payloads_by_type(store)
        self.assertIn("agent.tool_called", types)
        # GUEST 路径下 tier audit 字段仍写 "GUEST"
        self.assertEqual(
            types["agent.tool_called"][0]["triggered_by_user_tier"], "GUEST"
        )


class NoRegistryNoGateTest(unittest.IsolatedAsyncioTestCase):
    async def test_no_registry_means_no_gate(self) -> None:
        """旧 skeleton / 测试场景注入 tool_registry=None 时闸门退化 ——
        所有 call_tool 直接通过。"""
        store: list[Any] = []
        await _dispatch(
            _loop(registry=None, store=store),
            CallToolAction(
                tool_name="anything",
                arguments={},
            ),
            _ctx(bot_role=None),
        )
        types = _payloads_by_type(store)
        self.assertIn("agent.tool_called", types)
        self.assertNotIn("agent.tool_failed", types)


if __name__ == "__main__":
    unittest.main()

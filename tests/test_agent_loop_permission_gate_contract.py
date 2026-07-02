"""Contract tests for AgentLoop tool dispatch（权限闸门已全部下放工具）。

权限模型现状：AgentLoop **不做任何 role / tier / scope 判定**——它只把工具调用
所需的触发线索注入 ``agent.tool_called.payload``，判定全交给工具内的
``BaseTool.enforce_access``（enforce_scope + 现场解析发起人 tier + enforce_bot_admin）。

故本组测试钉死的是 loop 的"无为"：
- 任何工具 / 任何 bot_role / 任何 scope → loop 都照常 dispatch ``tool_called``，
  从不写 ``tool_failed``（scope/tier/bot 角色的实际拒绝发生在工具内）。
- payload 注入 ``triggered_by_event_id``（action 给的；缺则回退到 task anchor 补
  因果链）+ ``bot_role``。
- payload **不含** ``triggered_by_user_tier`` / ``triggered_by_user_id``——loop
  不再解析触发用户 tier（移交工具 enforce_permission 现场解析）。

发起人 tier / bot 角色 / scope 的实际拒绝由各工具 contract 测试 +
test_tool_permission_enforcement_contract 元测试覆盖。
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
        return SimpleNamespace(rowcount=1, scalar_one_or_none=lambda: None)

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


class _AdminTool:
    """敏感工具：ADMIN + 须 bot admin。loop 不再据此拦任何东西（仅工具内判）。"""

    name = "kick_member"
    description = "kick"
    arguments_schema = {"type": "object"}
    required_permission = PermissionTier.ADMIN
    require_bot_admin = True

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


def _ctx(
    *, bot_role: str | None, active_tasks: list[TaskView] | None = None
) -> DecisionContext:
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
        planner=mock.Mock(),
        session_factory=_factory_for(store),
        tool_registry=registry,
    )


async def _dispatch(
    loop: AgentLoop, action: CallToolAction, context: DecisionContext
) -> None:
    await loop._apply_actions([action], "CID", "DID", context)


class LoopIsPermissionAgnosticTest(unittest.IsolatedAsyncioTestCase):
    async def test_sensitive_tool_dispatched_without_gate(self) -> None:
        # 敏感工具 + bot 非 admin：loop 仍 dispatch、绝不写 tool_failed。
        reg = ToolRegistry()
        reg.register(_AdminTool())
        store: list[Any] = []
        await _dispatch(
            _loop(registry=reg, store=store),
            CallToolAction(
                tool_name="kick_member",
                arguments={"user_id": 1},
                triggered_by_event_id="E1",
            ),
            _ctx(bot_role="member"),
        )
        types = _payloads_by_type(store)
        self.assertIn("agent.tool_called", types)
        self.assertNotIn("agent.tool_failed", types)
        called = types["agent.tool_called"][0]
        self.assertEqual(called["bot_role"], "member")
        self.assertEqual(called["triggered_by_event_id"], "E1")

    async def test_payload_omits_resolved_tier(self) -> None:
        # loop 不再解析触发用户 tier：payload 不含 tier / user_id 字段。
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
            _ctx(bot_role="admin"),
        )
        called = _payloads_by_type(store)["agent.tool_called"][0]
        self.assertNotIn("triggered_by_user_tier", called)
        self.assertNotIn("triggered_by_user_id", called)

    async def test_no_resolution_call(self) -> None:
        # loop 既不 import 也不调用 resolve_user_tier_from_event；patch 它，断言
        # 从未被 await（解析已彻底移交工具）。
        reg = ToolRegistry()
        reg.register(_AdminTool())
        store: list[Any] = []
        with mock.patch(
            "qqbot.core.permissions.resolve_user_tier_from_event",
            new=mock.AsyncMock(),
        ) as patched:
            await _dispatch(
                _loop(registry=reg, store=store),
                CallToolAction(
                    tool_name="kick_member",
                    arguments={},
                    triggered_by_event_id="E1",
                ),
                _ctx(bot_role="admin"),
            )
            patched.assert_not_awaited()

    async def test_fallback_to_task_anchor_when_action_missing(self) -> None:
        # action 没填 triggered_by_event_id，但工具挂的 task 有 anchor →
        # loop 用 task anchor 补 triggered_by_event_id（因果链），不解析 tier。
        from qqbot.core.time import china_now

        reg = ToolRegistry()
        reg.register(_AdminTool())
        store: list[Any] = []
        ctx = _ctx(
            bot_role="admin",
            active_tasks=[
                TaskView(
                    task_id="T1",
                    scope_key="group:100",
                    description="anchor task",
                    related_tools=["kick_member"],
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
        await _dispatch(
            _loop(registry=reg, store=store),
            CallToolAction(tool_name="kick_member", arguments={}, task_id="T1"),
            ctx,
        )
        called = _payloads_by_type(store)["agent.tool_called"][0]
        self.assertEqual(called["triggered_by_event_id"], "E_anchor")

    async def test_guest_tool_dispatched(self) -> None:
        reg = ToolRegistry()
        reg.register(_GuestTool())
        store: list[Any] = []
        await _dispatch(
            _loop(registry=reg, store=store),
            CallToolAction(tool_name="echo", arguments={"x": 1}),
            _ctx(bot_role="member"),
        )
        types = _payloads_by_type(store)
        self.assertIn("agent.tool_called", types)
        self.assertNotIn("agent.tool_failed", types)
        self.assertIsNone(
            types["agent.tool_called"][0]["triggered_by_event_id"]
        )

    async def test_no_registry_still_dispatches(self) -> None:
        # registry=None（loop 不再依赖工具元数据做 dispatch）→ 照常 dispatch。
        store: list[Any] = []
        await _dispatch(
            _loop(registry=None, store=store),
            CallToolAction(tool_name="anything", arguments={}),
            _ctx(bot_role=None),
        )
        types = _payloads_by_type(store)
        self.assertIn("agent.tool_called", types)
        self.assertNotIn("agent.tool_failed", types)
        self.assertIsNone(types["agent.tool_called"][0]["bot_role"])


if __name__ == "__main__":
    unittest.main()

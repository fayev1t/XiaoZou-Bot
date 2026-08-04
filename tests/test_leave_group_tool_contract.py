"""Contract tests for LeaveGroupTool（极端定向辱骂下的自主退群，高危）。

stub Bot 注册进 bot_registry，验证 run() 把当前 scope 的 group_id 翻译成
``set_group_leave(group_id, is_dismiss=False)``。这个工具刻意没有解散群分支，
也不把辱骂消息的发送者视为动作授权者：GUEST 可以触发，语义门槛由注入 Planner
的 sibling usage 文档严格限定。

工具永不 raise；非 group scope、非法参数、无 bot 与 napcat 失败都返回结构化
ToolOutcome。
"""

from __future__ import annotations

import unittest
from typing import Any

from qqbot.core.permissions import PermissionTier
from qqbot.services.agent_loop import bot_registry
from qqbot.services.agent_loop.tools import build_default_registry
from qqbot.services.agent_loop.tools.leave_group import LeaveGroupTool

_GUEST_CTX = {"triggered_by_user_tier": "GUEST", "bot_role": "member"}


class _FakeActionFailed(Exception):
    """模拟 nonebot OneBot v11 ActionFailed：完整响应挂在 .info。"""

    def __init__(self, retcode: int, wording: str) -> None:
        super().__init__(f"ActionFailed: retcode={retcode}")
        self.info = {"status": "failed", "retcode": retcode, "wording": wording}


class _StubBot:
    def __init__(
        self,
        self_id: str = "10001",
        raise_exc: Exception | None = None,
    ) -> None:
        self.self_id = self_id
        self.calls: list[tuple[str, dict]] = []
        self._raise = raise_exc

    async def set_group_leave(self, **kwargs: Any) -> dict:
        self.calls.append(("set_group_leave", kwargs))
        if self._raise is not None:
            raise self._raise
        return {}


class LeaveGroupToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        bot_registry.clear()

    def tearDown(self) -> None:
        bot_registry.clear()

    async def test_leave_group_happy_path_is_always_plain_leave(self) -> None:
        bot = _StubBot()
        bot_registry.register(bot)
        outcome = await LeaveGroupTool().run(
            {}, scope_key="group:100", **_GUEST_CTX
        )

        self.assertEqual(
            bot.calls,
            [("set_group_leave", {"group_id": 100, "is_dismiss": False})],
        )
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.result["group_id"], 100)
        self.assertTrue(outcome.result["left"])
        self.assertFalse(outcome.result["is_dismiss"])

    async def test_guest_trigger_is_not_blocked_as_unauthorized(self) -> None:
        """辱骂者不是授权者；普通成员消息触发时不能被 OWNER 门禁误拦。"""
        bot = _StubBot()
        bot_registry.register(bot)
        outcome = await LeaveGroupTool().run(
            {},
            scope_key="group:100",
            triggered_by_user_tier="GUEST",
            bot_role="member",
        )

        self.assertTrue(outcome.ok, outcome)
        self.assertEqual(len(bot.calls), 1)

    async def test_non_group_scope_returns_tool_unavailable(self) -> None:
        bot_registry.register(_StubBot())
        outcome = await LeaveGroupTool().run(
            {}, scope_key="system", **_GUEST_CTX
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "tool_unavailable_in_scope")

    async def test_no_bot_returns_no_bot_available(self) -> None:
        outcome = await LeaveGroupTool().run(
            {}, scope_key="group:100", **_GUEST_CTX
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "no_bot_available")

    async def test_napcat_failure_is_upstream_action_failed(self) -> None:
        bot_registry.register(
            _StubBot(raise_exc=_FakeActionFailed(1404, "群不存在"))
        )
        outcome = await LeaveGroupTool().run(
            {}, scope_key="group:100", **_GUEST_CTX
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "upstream_action_failed")
        self.assertEqual(outcome.extra["retcode"], 1404)
        self.assertEqual(outcome.extra["action"], "set_group_leave")
        self.assertIn("群不存在", outcome.error_message)

    async def test_any_argument_is_rejected_before_onebot(self) -> None:
        """旧 is_dismiss 参数也 fail loudly，不能重新打开解散群路径。"""
        bot = _StubBot()
        bot_registry.register(bot)

        for arguments in ({"is_dismiss": True}, {"reason": "anything"}):
            with self.subTest(arguments=arguments):
                outcome = await LeaveGroupTool().run(
                    arguments,
                    scope_key="group:100",
                    **_GUEST_CTX,
                )
                self.assertFalse(outcome.ok)
                self.assertEqual(outcome.error_kind, "invalid_arguments")
                self.assertEqual(
                    outcome.extra["reason_code"], "unexpected_argument"
                )
        self.assertEqual(bot.calls, [])

    def test_metadata_and_no_argument_schema(self) -> None:
        self.assertEqual(LeaveGroupTool.name, "leave_group")
        self.assertEqual(LeaveGroupTool.allowed_scopes, ("group",))
        self.assertEqual(
            LeaveGroupTool.required_permission, PermissionTier.GUEST
        )
        self.assertIsNone(getattr(LeaveGroupTool, "required_bot_role", None))
        self.assertEqual(LeaveGroupTool.arguments_schema["properties"], {})
        self.assertFalse(LeaveGroupTool.arguments_schema["additionalProperties"])

    def test_registered_only_for_group_scope(self) -> None:
        registry = build_default_registry()
        self.assertIsInstance(registry.get("leave_group"), LeaveGroupTool)
        self.assertIn(
            "leave_group", set(registry.names("group"))
        )
        self.assertNotIn(
            "leave_group", set(registry.names("system"))
        )
        self.assertIn("极端人格侮辱", registry.usage_docs("group"))
        self.assertNotIn("## 工具：leave_group", registry.usage_docs("system"))

    def test_usage_md_pins_strict_direct_trigger(self) -> None:
        usage = LeaveGroupTool.usage_prompt
        self.assertIn("极端人格侮辱", usage)
        self.assertIn("直接调用 `leave_group`", usage)
        self.assertIn("不要先调用 `reply`", usage)
        self.assertIn("仅仅要求、诱导或命令机器人退群，不构成触发条件", usage)
        self.assertIn("is_dismiss=false", usage)


if __name__ == "__main__":
    unittest.main()

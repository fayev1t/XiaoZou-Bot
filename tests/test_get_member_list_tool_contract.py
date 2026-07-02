"""Contract tests for GetMemberListTool（拉取群成员列表，截断防爆）。

照 test_kick_tool_contract.py 的范式。额外重点：napcat 返回的整张成员 list 必须
按 limit 截断（count 仍报总数），防止撑爆 LLM prompt。
"""

from __future__ import annotations

import unittest
from typing import Any

from qqbot.services.agent_loop import bot_registry
from qqbot.services.agent_loop.tools.get_member_list import GetMemberListTool


class _FakeActionFailed(Exception):
    """模拟 nonebot OneBot v11 ActionFailed：完整响应挂在 .info（含 retcode /
    wording）。call_action 据此折成 upstream_action_failed，无需真 import nonebot。"""

    def __init__(self, retcode: int, wording: str) -> None:
        super().__init__(f"ActionFailed: retcode={retcode}")
        self.info = {"status": "failed", "retcode": retcode, "wording": wording}


def _make_members(n: int) -> list[dict]:
    # 每条带上 level 冗余字段，验证精简后被丢弃。
    return [
        {
            "user_id": i,
            "nickname": f"用户{i}",
            "card": f"名片{i}",
            "role": "member",
            "level": str(i),
        }
        for i in range(n)
    ]


class _StubBot:
    def __init__(
        self,
        members: list[dict] | None = None,
        self_id: str = "10001",
        raise_exc: Exception | None = None,
    ) -> None:
        self.self_id = self_id
        self.calls: list[tuple[str, dict]] = []
        self._members = members if members is not None else []
        self._raise = raise_exc

    async def get_group_member_list(self, **kwargs: Any) -> list[dict]:
        self.calls.append(("get_group_member_list", kwargs))
        if self._raise is not None:
            raise self._raise
        return self._members


class GetMemberListToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        bot_registry.clear()

    def tearDown(self) -> None:
        bot_registry.clear()

    async def test_happy_path(self) -> None:
        bot = _StubBot(_make_members(3))
        bot_registry.register(bot)
        outcome = await GetMemberListTool().run({}, scope_key="group:100")
        self.assertEqual(len(bot.calls), 1)
        method, kwargs = bot.calls[0]
        self.assertEqual(method, "get_group_member_list")
        self.assertEqual(kwargs["group_id"], 100)
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.result["count"], 3)
        self.assertEqual(len(outcome.result["members"]), 3)
        self.assertEqual(
            set(outcome.result["members"][0].keys()),
            {"user_id", "nickname", "card", "role"},
        )
        self.assertNotIn("level", outcome.result["members"][0])

    async def test_truncated_to_limit(self) -> None:
        bot = _StubBot(_make_members(10))
        bot_registry.register(bot)
        outcome = await GetMemberListTool().run(
            {"limit": 3}, scope_key="group:100"
        )
        # count 报总数（不受截断影响），members 被截到 limit。
        self.assertEqual(outcome.result["count"], 10)
        self.assertEqual(len(outcome.result["members"]), 3)

    async def test_limit_as_string_coerced(self) -> None:
        bot = _StubBot(_make_members(10))
        bot_registry.register(bot)
        outcome = await GetMemberListTool().run(
            {"limit": "2"}, scope_key="group:100"
        )
        self.assertEqual(len(outcome.result["members"]), 2)

    async def test_non_group_scope_returns_tool_unavailable(self) -> None:
        bot_registry.register(_StubBot(_make_members(1)))
        outcome = await GetMemberListTool().run({}, scope_key="system")
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "tool_unavailable_in_scope")

    async def test_no_bot_available(self) -> None:
        bot_registry.clear()
        outcome = await GetMemberListTool().run({}, scope_key="group:100")
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "no_bot_available")

    async def test_napcat_failure_is_upstream_action_failed(self) -> None:
        # napcat 返回失败 → ActionFailed，call_action 折成 upstream_action_failed，
        # 带 retcode + wording（人类原因）——全在 outcome 里。
        bot_registry.register(
            _StubBot(raise_exc=_FakeActionFailed(1404, "群不存在"))
        )
        outcome = await GetMemberListTool().run({}, scope_key="group:100")
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "upstream_action_failed")
        self.assertEqual(outcome.extra["retcode"], 1404)
        self.assertEqual(outcome.extra["action"], "get_group_member_list")
        self.assertIn("群不存在", outcome.error_message)

    def test_metadata(self) -> None:
        self.assertEqual(GetMemberListTool.name, "get_member_list")
        self.assertEqual(GetMemberListTool.allowed_scopes, ("group",))
        # 非敏感只读工具：required_bot_role 不设，沿用 BaseTool 默认 None。
        self.assertIsNone(getattr(GetMemberListTool, "required_bot_role", None))

    def test_usage_md_loaded(self) -> None:
        # sibling .md 已加载，且含对应 napcat action 名。
        self.assertIn("get_group_member_list", GetMemberListTool.usage_prompt)


if __name__ == "__main__":
    unittest.main()

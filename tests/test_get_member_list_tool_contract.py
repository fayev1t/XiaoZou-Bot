"""Contract tests for GetMemberListTool（拉取群成员列表，截断防爆）。

照 test_kick_tool_contract.py 的范式。额外重点：napcat 返回的整张成员 list 必须
按 limit 截断（count 仍报总数），防止撑爆 LLM prompt。

2026-07-07 重做恢复的增强契约：role 过滤在截断**之前**（"列出所有管理员"不被
limit 吃掉）；include_activity 附带 ISO 化的 join_time/last_sent_time；正被禁言
（shut_up_timestamp 在未来）的成员附带 banned_until，其余不占键。
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
        # 无 role 过滤时 matched == count。
        self.assertEqual(outcome.result["matched"], 3)
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

    async def test_role_filter_applies_before_truncation(self) -> None:
        members = _make_members(8)
        members.append(
            {"user_id": 100, "nickname": "管理甲", "card": "", "role": "admin"}
        )
        members.append(
            {"user_id": 101, "nickname": "管理乙", "card": "", "role": "Admin"}
        )
        bot_registry.register(_StubBot(members))
        # limit=1 也必须先过滤再截断：matched 报全部命中数，不被 limit 吃掉。
        outcome = await GetMemberListTool().run(
            {"role": "admin", "limit": 1}, scope_key="group:100"
        )
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.result["count"], 10)
        self.assertEqual(outcome.result["matched"], 2)  # 大小写不敏感
        self.assertEqual(len(outcome.result["members"]), 1)
        self.assertEqual(outcome.result["members"][0]["user_id"], 100)

    async def test_invalid_role_rejected(self) -> None:
        bot_registry.register(_StubBot(_make_members(1)))
        outcome = await GetMemberListTool().run(
            {"role": "boss"}, scope_key="group:100"
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "invalid_arguments")

    async def test_include_activity_adds_iso_times(self) -> None:
        members = [
            {
                "user_id": 1,
                "nickname": "甲",
                "card": "",
                "role": "member",
                "join_time": 1700000000,
                "last_sent_time": 0,  # napcat 缺值常给 0 → None
            }
        ]
        bot_registry.register(_StubBot(members))
        outcome = await GetMemberListTool().run(
            {"include_activity": True}, scope_key="group:100"
        )
        self.assertTrue(outcome.ok)
        entry = outcome.result["members"][0]
        self.assertEqual(entry["join_time"], "2023-11-15T06:13:20+08:00")
        self.assertIsNone(entry["last_sent_time"])

    async def test_banned_member_carries_banned_until(self) -> None:
        members = [
            {
                "user_id": 1,
                "nickname": "被禁言",
                "card": "",
                "role": "member",
                "shut_up_timestamp": 4102444800,  # 2100-01-01，禁言中
            },
            {
                "user_id": 2,
                "nickname": "已过期",
                "card": "",
                "role": "member",
                "shut_up_timestamp": 1000,  # 早已过期 → 不占键
            },
        ]
        bot_registry.register(_StubBot(members))
        outcome = await GetMemberListTool().run({}, scope_key="group:100")
        self.assertTrue(outcome.ok)
        banned, expired = outcome.result["members"]
        self.assertTrue(banned["banned_until"].startswith("2100-01-01"))
        self.assertNotIn("banned_until", expired)

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

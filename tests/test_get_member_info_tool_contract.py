"""Contract tests for GetMemberInfoTool（查询群成员资料）。

照 test_kick_tool_contract.py 的范式：stub Bot 注册进 bot_registry，验证 run()
把 group_id（从 scope_key 注入）+ user_id 翻译成 get_group_member_info 调用，并
把 napcat 原始结果精简成约定字段；非 group scope / 缺参 / 无 bot 各自返回失败 outcome。
"""

from __future__ import annotations

import unittest
from typing import Any

from qqbot.services.agent_loop import bot_registry
from qqbot.services.agent_loop.tools.get_member_info import GetMemberInfoTool


class _FakeActionFailed(Exception):
    """模拟 nonebot OneBot v11 ActionFailed：完整响应挂在 .info（含 retcode /
    wording）。call_action 据此折成 upstream_action_failed，无需真 import nonebot。"""

    def __init__(self, retcode: int, wording: str) -> None:
        super().__init__(f"ActionFailed: retcode={retcode}")
        self.info = {"status": "failed", "retcode": retcode, "wording": wording}


class _StubBot:
    def __init__(
        self,
        self_id: str = "10001",
        raise_exc: Exception | None = None,
        info: dict | None = None,
    ) -> None:
        self.self_id = self_id
        self.calls: list[tuple[str, dict]] = []
        self._raise = raise_exc
        # 故意带上 sex/area 等冗余字段，验证它们被精简丢弃；
        # shut_up_timestamp 已过期 → banned_until 应为 None。
        self._info = info if info is not None else {
            "user_id": 222,
            "nickname": "阿狸",
            "card": "群名片",
            "role": "admin",
            "level": "30",
            "title": "头衔",
            "join_time": 1600000000,
            "last_sent_time": 1700000000,
            "shut_up_timestamp": 1000,
            "sex": "female",
            "area": "上海",
        }

    async def get_group_member_info(self, **kwargs: Any) -> dict:
        self.calls.append(("get_group_member_info", kwargs))
        if self._raise is not None:
            raise self._raise
        return self._info


class GetMemberInfoToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        bot_registry.clear()

    def tearDown(self) -> None:
        bot_registry.clear()

    async def test_happy_path(self) -> None:
        bot = _StubBot()
        bot_registry.register(bot)
        outcome = await GetMemberInfoTool().run(
            {"user_id": 222}, scope_key="group:100"
        )
        self.assertEqual(len(bot.calls), 1)
        method, kwargs = bot.calls[0]
        self.assertEqual(method, "get_group_member_info")
        self.assertEqual(kwargs["group_id"], 100)
        self.assertEqual(kwargs["user_id"], 222)
        # 实时性优先：权限/禁言核对不吃缓存（2026-07-07 重做恢复时改为 True）。
        self.assertTrue(kwargs["no_cache"])
        self.assertTrue(outcome.ok)
        self.assertEqual(
            set(outcome.result.keys()),
            {
                "user_id",
                "nickname",
                "card",
                "role",
                "level",
                "title",
                "join_time",
                "last_sent_time",
                "banned_until",
            },
        )
        self.assertEqual(outcome.result["nickname"], "阿狸")
        self.assertEqual(outcome.result["role"], "admin")
        # 裸 epoch → Asia/Shanghai ISO（LLM 不心算 epoch）。
        self.assertEqual(outcome.result["join_time"], "2020-09-13T20:26:40+08:00")
        self.assertEqual(
            outcome.result["last_sent_time"], "2023-11-15T06:13:20+08:00"
        )
        # shut_up_timestamp 已过期 → 不在禁言中 → None（键恒在）。
        self.assertIsNone(outcome.result["banned_until"])
        self.assertNotIn("sex", outcome.result)
        self.assertNotIn("area", outcome.result)

    async def test_currently_muted_member_has_banned_until(self) -> None:
        bot_registry.register(
            _StubBot(
                info={
                    "user_id": 333,
                    "nickname": "被禁言",
                    "card": "",
                    "role": "member",
                    "shut_up_timestamp": 4102444800,  # 2100-01-01，禁言中
                }
            )
        )
        outcome = await GetMemberInfoTool().run(
            {"user_id": 333}, scope_key="group:100"
        )
        self.assertTrue(outcome.ok)
        self.assertTrue(outcome.result["banned_until"].startswith("2100-01-01"))
        # 平台没给的时间字段 → None，不猜。
        self.assertIsNone(outcome.result["join_time"])

    async def test_user_id_as_string_coerced(self) -> None:
        bot = _StubBot()
        bot_registry.register(bot)
        await GetMemberInfoTool().run({"user_id": "222"}, scope_key="group:100")
        self.assertEqual(bot.calls[0][1]["user_id"], 222)

    async def test_non_group_scope_returns_tool_unavailable(self) -> None:
        bot_registry.register(_StubBot())
        outcome = await GetMemberInfoTool().run(
            {"user_id": 1}, scope_key="system"
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "tool_unavailable_in_scope")

    async def test_missing_user_id_invalid_arguments(self) -> None:
        bot_registry.register(_StubBot())
        outcome = await GetMemberInfoTool().run({}, scope_key="group:100")
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "invalid_arguments")

    async def test_no_bot_available(self) -> None:
        bot_registry.clear()
        outcome = await GetMemberInfoTool().run(
            {"user_id": 1}, scope_key="group:100"
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "no_bot_available")

    async def test_napcat_failure_is_upstream_action_failed(self) -> None:
        # napcat 返回失败（成员不存在等）→ ActionFailed，call_action 折成
        # upstream_action_failed，带 retcode + wording（人类原因）——全在 outcome 里。
        bot_registry.register(
            _StubBot(raise_exc=_FakeActionFailed(1404, "群成员不存在"))
        )
        outcome = await GetMemberInfoTool().run(
            {"user_id": 1}, scope_key="group:100"
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "upstream_action_failed")
        self.assertEqual(outcome.extra["retcode"], 1404)
        self.assertEqual(outcome.extra["action"], "get_group_member_info")
        self.assertIn("群成员不存在", outcome.error_message)

    def test_metadata(self) -> None:
        self.assertEqual(GetMemberInfoTool.name, "get_member_info")
        self.assertEqual(GetMemberInfoTool.allowed_scopes, ("group",))
        # 非敏感只读工具：required_bot_role 不设，沿用 BaseTool 默认 None。
        self.assertIsNone(getattr(GetMemberInfoTool, "required_bot_role", None))

    def test_usage_md_loaded(self) -> None:
        # sibling .md 已加载，且含对应 napcat action 名。
        self.assertIn("get_group_member_info", GetMemberInfoTool.usage_prompt)


if __name__ == "__main__":
    unittest.main()

"""Contract tests for GetGroupHonorTool（查询群荣誉榜）。

照 test_kick_tool_contract.py 的范式。额外重点：各榜单只保留前 5 条
（current_talkative 单独保留），每条精简成 user_id/nickname/description，防止整张
榜单撑爆 LLM prompt；type 透传给 napcat。
"""

from __future__ import annotations

import unittest
from typing import Any

from qqbot.services.agent_loop import bot_registry
from qqbot.services.agent_loop.tools.get_group_honor import GetGroupHonorTool


class _FakeActionFailed(Exception):
    """模拟 nonebot OneBot v11 ActionFailed：完整响应挂在 .info（含 retcode /
    wording）。call_action 据此折成 upstream_action_failed，无需真 import nonebot。"""

    def __init__(self, retcode: int, wording: str) -> None:
        super().__init__(f"ActionFailed: retcode={retcode}")
        self.info = {"status": "failed", "retcode": retcode, "wording": wording}


class _StubBot:
    def __init__(
        self, self_id: str = "10001", raise_exc: Exception | None = None
    ) -> None:
        self.self_id = self_id
        self.calls: list[tuple[str, dict]] = []
        self._raise = raise_exc

    async def get_group_honor_info(self, **kwargs: Any) -> dict:
        self.calls.append(("get_group_honor_info", kwargs))
        if self._raise is not None:
            raise self._raise
        return {
            "group_id": 100,
            "current_talkative": {
                "user_id": 1,
                "nickname": "龙王",
                "avatar": "http://x",
                "day_count": 7,
            },
            # 7 条 → 应被截到 5 条；每条带 avatar 冗余字段验证被丢弃。
            "talkative_list": [
                {
                    "user_id": i,
                    "nickname": f"u{i}",
                    "avatar": "http://x",
                    "description": f"d{i}",
                }
                for i in range(7)
            ],
            # 3 条 → 不足 5，原样保留。
            "performer_list": [
                {
                    "user_id": 100 + i,
                    "nickname": f"p{i}",
                    "description": f"pd{i}",
                }
                for i in range(3)
            ],
        }


class GetGroupHonorToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        bot_registry.clear()

    def tearDown(self) -> None:
        bot_registry.clear()

    async def test_happy_path(self) -> None:
        bot = _StubBot()
        bot_registry.register(bot)
        outcome = await GetGroupHonorTool().run({}, scope_key="group:100")
        self.assertEqual(len(bot.calls), 1)
        method, kwargs = bot.calls[0]
        self.assertEqual(method, "get_group_honor_info")
        self.assertEqual(kwargs["group_id"], 100)
        # 缺省 type=all。
        self.assertEqual(kwargs["type"], "all")
        self.assertTrue(outcome.ok)

        # current_talkative 保留且精简。
        current = outcome.result["current_talkative"]
        self.assertEqual(
            set(current.keys()), {"user_id", "nickname", "description"}
        )
        self.assertEqual(current["user_id"], 1)
        self.assertEqual(current["nickname"], "龙王")

        # talkative_list 7 条截到 5；每条丢弃 avatar。
        self.assertEqual(len(outcome.result["talkative_list"]), 5)
        self.assertEqual(
            set(outcome.result["talkative_list"][0].keys()),
            {"user_id", "nickname", "description"},
        )
        self.assertNotIn("avatar", outcome.result["talkative_list"][0])

        # performer_list 3 条不足 5，原样保留。
        self.assertEqual(len(outcome.result["performer_list"]), 3)

    async def test_type_passthrough(self) -> None:
        bot = _StubBot()
        bot_registry.register(bot)
        await GetGroupHonorTool().run(
            {"type": "talkative"}, scope_key="group:100"
        )
        self.assertEqual(bot.calls[0][1]["type"], "talkative")

    async def test_non_group_scope_returns_tool_unavailable(self) -> None:
        bot_registry.register(_StubBot())
        outcome = await GetGroupHonorTool().run({}, scope_key="system")
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "tool_unavailable_in_scope")

    async def test_no_bot_available(self) -> None:
        bot_registry.clear()
        outcome = await GetGroupHonorTool().run({}, scope_key="group:100")
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "no_bot_available")

    async def test_napcat_failure_is_upstream_action_failed(self) -> None:
        # napcat 返回失败 → ActionFailed，call_action 折成 upstream_action_failed，
        # 带 retcode + wording（人类原因）——全在 outcome 里。
        bot_registry.register(
            _StubBot(raise_exc=_FakeActionFailed(1404, "群不存在"))
        )
        outcome = await GetGroupHonorTool().run({}, scope_key="group:100")
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "upstream_action_failed")
        self.assertEqual(outcome.extra["retcode"], 1404)
        self.assertEqual(outcome.extra["action"], "get_group_honor_info")
        self.assertIn("群不存在", outcome.error_message)

    def test_metadata(self) -> None:
        self.assertEqual(GetGroupHonorTool.name, "get_group_honor")
        self.assertEqual(GetGroupHonorTool.allowed_scopes, ("group",))
        # 非敏感只读工具：required_bot_role 不设，沿用 BaseTool 默认 None。
        self.assertIsNone(getattr(GetGroupHonorTool, "required_bot_role", None))

    def test_usage_md_loaded(self) -> None:
        # sibling .md 已加载，且含对应 napcat action 名。
        self.assertIn("get_group_honor_info", GetGroupHonorTool.usage_prompt)


if __name__ == "__main__":
    unittest.main()

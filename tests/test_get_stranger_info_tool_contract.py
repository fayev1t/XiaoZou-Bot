"""Contract tests for GetStrangerInfoTool（查询任意 QQ 公开资料）。

本工具不限 scope（查 QQ 公开资料不依赖群），所以**不测** scope 限制——happy
path 用 scope_key="group:1" 即可。其余照 test_kick_tool_contract.py：验证 run()
把 user_id 翻译成 get_stranger_info 调用并精简结果；缺参 / 无 bot 各自返回失败 outcome。
"""

from __future__ import annotations

import unittest
from typing import Any

from qqbot.services.agent_loop import bot_registry
from qqbot.services.agent_loop.tools.get_stranger_info import (
    GetStrangerInfoTool,
)


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

    async def get_stranger_info(self, **kwargs: Any) -> dict:
        self.calls.append(("get_stranger_info", kwargs))
        if self._raise is not None:
            raise self._raise
        # qid / level 是冗余字段，验证被精简丢弃。
        return {
            "user_id": 333,
            "nickname": "Bob",
            "sex": "male",
            "age": 25,
            "qid": "abc",
            "level": 10,
        }


class GetStrangerInfoToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        bot_registry.clear()

    def tearDown(self) -> None:
        bot_registry.clear()

    async def test_happy_path(self) -> None:
        bot = _StubBot()
        bot_registry.register(bot)
        outcome = await GetStrangerInfoTool().run(
            {"user_id": 333}, scope_key="group:1"
        )
        self.assertEqual(len(bot.calls), 1)
        method, kwargs = bot.calls[0]
        self.assertEqual(method, "get_stranger_info")
        self.assertEqual(kwargs["user_id"], 333)
        self.assertFalse(kwargs["no_cache"])
        self.assertTrue(outcome.ok)
        self.assertEqual(
            set(outcome.result.keys()), {"user_id", "nickname", "sex", "age"}
        )
        self.assertEqual(outcome.result["nickname"], "Bob")
        self.assertEqual(outcome.result["age"], 25)
        self.assertNotIn("qid", outcome.result)
        self.assertNotIn("level", outcome.result)

    async def test_user_id_as_string_coerced(self) -> None:
        bot = _StubBot()
        bot_registry.register(bot)
        await GetStrangerInfoTool().run(
            {"user_id": "333"}, scope_key="group:1"
        )
        self.assertEqual(bot.calls[0][1]["user_id"], 333)

    async def test_missing_user_id_invalid_arguments(self) -> None:
        bot_registry.register(_StubBot())
        outcome = await GetStrangerInfoTool().run({}, scope_key="group:1")
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "invalid_arguments")

    async def test_no_bot_available(self) -> None:
        bot_registry.clear()
        outcome = await GetStrangerInfoTool().run(
            {"user_id": 1}, scope_key="group:1"
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "no_bot_available")

    async def test_napcat_failure_is_upstream_action_failed(self) -> None:
        # napcat 返回失败（查不到等）→ ActionFailed，call_action 折成
        # upstream_action_failed，带 retcode + wording（人类原因）——全在 outcome 里。
        bot_registry.register(
            _StubBot(raise_exc=_FakeActionFailed(1404, "用户不存在"))
        )
        outcome = await GetStrangerInfoTool().run(
            {"user_id": 1}, scope_key="group:1"
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "upstream_action_failed")
        self.assertEqual(outcome.extra["retcode"], 1404)
        self.assertEqual(outcome.extra["action"], "get_stranger_info")
        self.assertIn("用户不存在", outcome.error_message)

    def test_metadata(self) -> None:
        self.assertEqual(GetStrangerInfoTool.name, "get_stranger_info")
        # 不限 scope：allowed_scopes 沿用 BaseTool 默认 None。
        self.assertIsNone(GetStrangerInfoTool.allowed_scopes)
        # 非敏感只读工具：required_bot_role 不设，沿用 BaseTool 默认 None。
        self.assertIsNone(
            getattr(GetStrangerInfoTool, "required_bot_role", None)
        )

    def test_usage_md_loaded(self) -> None:
        # sibling .md 已加载，且含对应 napcat action 名。
        self.assertIn("get_stranger_info", GetStrangerInfoTool.usage_prompt)


if __name__ == "__main__":
    unittest.main()

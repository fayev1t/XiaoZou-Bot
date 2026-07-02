"""Contract tests for PokeTool（戳一戳群成员）。

同 KickTool 范式：stub Bot 注册进 bot_registry，验证 run() 把 group_id（从
scope_key 注入）+ arguments 正确翻译成 napcat group_poke 调用；非 group scope
/ 缺参 / 无 bot 各自返回结构化失败 outcome（outcome.error_kind）。
"""

from __future__ import annotations

import unittest
from typing import Any

from qqbot.core.permissions import PermissionTier
from qqbot.services.agent_loop import bot_registry
from qqbot.services.agent_loop.tools.poke import PokeTool


class _FakeActionFailed(Exception):
    """模拟 nonebot OneBot v11 ActionFailed：完整响应挂在 .info（含 retcode /
    wording）。call_action 据此折成 upstream_action_failed，无需真 import nonebot。"""

    def __init__(self, retcode: int, wording: str) -> None:
        super().__init__(f"ActionFailed: retcode={retcode}")
        self.info = {"status": "failed", "retcode": retcode, "wording": wording}


class _StubBot:
    def __init__(self, self_id: str = "10001", raise_exc: Exception | None = None) -> None:
        self.self_id = self_id
        self.calls: list[tuple[str, dict]] = []
        self._raise = raise_exc

    async def group_poke(self, **kwargs: Any) -> dict:
        self.calls.append(("group_poke", kwargs))
        if self._raise is not None:
            raise self._raise
        return {}


class PokeToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        bot_registry.clear()

    def tearDown(self) -> None:
        bot_registry.clear()

    async def test_poke_happy_path(self) -> None:
        bot = _StubBot()
        bot_registry.register(bot)
        outcome = await PokeTool().run(
            {"user_id": 222},
            scope_key="group:100",
        )
        self.assertEqual(len(bot.calls), 1)
        method, kwargs = bot.calls[0]
        self.assertEqual(method, "group_poke")
        self.assertEqual(kwargs["group_id"], 100)
        self.assertEqual(kwargs["user_id"], 222)
        # 结构化输出：succeeded → result
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.result["group_id"], 100)
        self.assertEqual(outcome.result["user_id"], 222)

    async def test_user_id_as_string_coerced(self) -> None:
        bot = _StubBot()
        bot_registry.register(bot)
        await PokeTool().run({"user_id": "222"}, scope_key="group:100")
        self.assertEqual(bot.calls[0][1]["user_id"], 222)

    async def test_non_group_scope_raises(self) -> None:
        # enforce_scope 在 run() 第一行（enforce_access）先于一切拦下越 scope 调用。
        bot_registry.register(_StubBot())
        outcome = await PokeTool().run({"user_id": 1}, scope_key="system")
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "tool_unavailable_in_scope")

    async def test_missing_user_id_raises(self) -> None:
        bot_registry.register(_StubBot())
        outcome = await PokeTool().run({}, scope_key="group:100")
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "invalid_arguments")

    async def test_no_bot_raises(self) -> None:
        bot_registry.clear()
        outcome = await PokeTool().run({"user_id": 1}, scope_key="group:100")
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "no_bot_available")

    async def test_napcat_failure_is_upstream_action_failed(self) -> None:
        # napcat 返回失败（目标不存在等）→ ActionFailed 冒泡，call_action 折成
        # upstream_action_failed，带 retcode + wording（人类原因）。
        bot_registry.register(
            _StubBot(raise_exc=_FakeActionFailed(1404, "目标不存在"))
        )
        outcome = await PokeTool().run({"user_id": 1}, scope_key="group:100")
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "upstream_action_failed")
        self.assertEqual(outcome.extra["retcode"], 1404)
        self.assertEqual(outcome.extra["action"], "group_poke")
        self.assertIn("目标不存在", outcome.error_message)

    def test_metadata(self) -> None:
        self.assertEqual(PokeTool.name, "poke")
        self.assertEqual(PokeTool.allowed_scopes, ("group",))
        self.assertEqual(PokeTool.required_permission, PermissionTier.GUEST)
        # 非敏感互动：bot 自身群角色不设限，保持 BaseTool 默认 None。
        self.assertIsNone(getattr(PokeTool, "required_bot_role", None))

    def test_usage_md_loaded(self) -> None:
        self.assertIn("group_poke", PokeTool.usage_prompt)


if __name__ == "__main__":
    unittest.main()

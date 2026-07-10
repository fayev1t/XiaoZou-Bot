"""Contract tests for GetGroupInfoTool（查询群基本信息）。

照 test_kick_tool_contract.py 的范式：验证 run() 把 group_id（从 scope_key 注入）
翻译成 get_group_info 调用，并把 napcat 原始结果精简成约定字段；非 group scope /
无 bot 各自返回失败 outcome。本工具无必填参数，故不测缺参。
"""

from __future__ import annotations

import unittest
from typing import Any

from qqbot.services.agent_loop import bot_registry
from qqbot.services.agent_loop.tools.get_group_info import GetGroupInfoTool


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
        # group_memo（→ 透传为 group_remark）/ group_create_time（→ ISO）是
        # 可选透传字段；group_level 是冗余字段，验证被精简丢弃。
        self._info = info if info is not None else {
            "group_id": 100,
            "group_name": "测试群",
            "member_count": 50,
            "max_member_count": 200,
            "group_memo": "公告",
            "group_create_time": 1700000000,
            "group_level": 1,
        }

    async def get_group_info(self, **kwargs: Any) -> dict:
        self.calls.append(("get_group_info", kwargs))
        if self._raise is not None:
            raise self._raise
        return self._info


class GetGroupInfoToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        bot_registry.clear()

    def tearDown(self) -> None:
        bot_registry.clear()

    async def test_happy_path(self) -> None:
        bot = _StubBot()
        bot_registry.register(bot)
        outcome = await GetGroupInfoTool().run({}, scope_key="group:100")
        self.assertEqual(len(bot.calls), 1)
        method, kwargs = bot.calls[0]
        self.assertEqual(method, "get_group_info")
        self.assertEqual(kwargs["group_id"], 100)
        # 实时性优先：人数查询不吃缓存（2026-07-07 重做恢复时改为 True）。
        self.assertTrue(kwargs["no_cache"])
        self.assertTrue(outcome.ok)
        self.assertEqual(
            set(outcome.result.keys()),
            {
                "group_id",
                "group_name",
                "member_count",
                "max_member_count",
                "group_remark",
                "group_create_time",
            },
        )
        self.assertEqual(outcome.result["group_name"], "测试群")
        self.assertEqual(outcome.result["member_count"], 50)
        # group_memo 候选键透传为 group_remark；建群时间转 Asia/Shanghai ISO。
        self.assertEqual(outcome.result["group_remark"], "公告")
        self.assertEqual(
            outcome.result["group_create_time"], "2023-11-15T06:13:20+08:00"
        )
        self.assertNotIn("group_memo", outcome.result)
        self.assertNotIn("group_level", outcome.result)

    async def test_optional_fields_absent_when_platform_omits(self) -> None:
        # NapCat 老版本常给 0/空——可选字段不占键，LLM 见键即可信。
        bot_registry.register(
            _StubBot(
                info={
                    "group_id": 100,
                    "group_name": "测试群",
                    "member_count": 50,
                    "max_member_count": 200,
                    "group_memo": "",
                    "group_create_time": 0,
                }
            )
        )
        outcome = await GetGroupInfoTool().run({}, scope_key="group:100")
        self.assertTrue(outcome.ok)
        self.assertEqual(
            set(outcome.result.keys()),
            {"group_id", "group_name", "member_count", "max_member_count"},
        )

    async def test_non_group_scope_returns_tool_unavailable(self) -> None:
        bot_registry.register(_StubBot())
        outcome = await GetGroupInfoTool().run({}, scope_key="system")
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "tool_unavailable_in_scope")

    async def test_no_bot_available(self) -> None:
        bot_registry.clear()
        outcome = await GetGroupInfoTool().run({}, scope_key="group:100")
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "no_bot_available")

    async def test_napcat_failure_is_upstream_action_failed(self) -> None:
        # napcat 返回失败 → ActionFailed，call_action 折成 upstream_action_failed，
        # 带 retcode + wording（人类原因）——全在 outcome 里。
        bot_registry.register(
            _StubBot(raise_exc=_FakeActionFailed(1404, "群不存在"))
        )
        outcome = await GetGroupInfoTool().run({}, scope_key="group:100")
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "upstream_action_failed")
        self.assertEqual(outcome.extra["retcode"], 1404)
        self.assertEqual(outcome.extra["action"], "get_group_info")
        self.assertIn("群不存在", outcome.error_message)

    def test_metadata(self) -> None:
        self.assertEqual(GetGroupInfoTool.name, "get_group_info")
        self.assertEqual(GetGroupInfoTool.allowed_scopes, ("group",))
        # 非敏感只读工具：required_bot_role 不设，沿用 BaseTool 默认 None。
        self.assertIsNone(getattr(GetGroupInfoTool, "required_bot_role", None))

    def test_usage_md_loaded(self) -> None:
        # sibling .md 已加载，且含对应 napcat action 名。
        self.assertIn("get_group_info", GetGroupInfoTool.usage_prompt)


if __name__ == "__main__":
    unittest.main()

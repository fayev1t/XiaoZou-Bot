"""Contract tests for GetPendingJoinRequestsTool（查询本群待处理入群申请）。

照 test_get_group_info_tool_contract.py 的范式：验证 run() 调 napcat 的
get_group_system_msg 并把账号全局的申请列表过滤/精简成本群视图。重点契约：

- 跨群条目、归属解析不出的条目、invited 类一律不出本群 scope（隔离 §9）；
- checked 已处理的只计数不出明细；
- flag / request_id **绝不**出现在返回值（审批凭证不经 LLM）；
- 字段名候选集同时认 go-cqhttp snake_case 与 NapCat 驼峰变体；
- 结构认不出 → upstream_payload_invalid（带 received_keys 供探针排查）；
- required_bot_role="admin"：bot 非管理员前置拦（stub bot 无
  get_group_member_info → 实时查失败回退 context.bot_role 快照，
  与 respond_to_group_join_request 测试同法）。
"""

from __future__ import annotations

import json
import unittest
from typing import Any

from qqbot.core.permissions import PermissionTier
from qqbot.services.agent_loop import bot_registry
from qqbot.services.agent_loop.tools.get_pending_join_requests import (
    GetPendingJoinRequestsTool,
)

# bot_role 快照给 admin：stub bot 没有 get_group_member_info，实时查失败后
# 回退这份快照，enforce_bot_admin 才放行（见 tool_registry._resolve_live_bot_role）。
_OK_CTX = {"scope_key": "group:100", "bot_role": "admin"}


class _FakeActionFailed(Exception):
    """模拟 nonebot OneBot v11 ActionFailed：完整响应挂在 .info（含 retcode /
    wording）。call_action 据此折成 upstream_action_failed，无需真 import nonebot。"""

    def __init__(self, retcode: int, wording: str) -> None:
        super().__init__(f"ActionFailed: retcode={retcode}")
        self.info = {"status": "failed", "retcode": retcode, "wording": wording}


class _StubBot:
    def __init__(
        self,
        response: Any = None,
        raise_exc: Exception | None = None,
        self_id: str = "10001",
    ) -> None:
        self.self_id = self_id
        self.calls: list[tuple[str, dict]] = []
        self._response = response
        self._raise = raise_exc

    async def get_group_system_msg(self, **kwargs: Any) -> Any:
        self.calls.append(("get_group_system_msg", kwargs))
        if self._raise is not None:
            raise self._raise
        return self._response


# go-cqhttp 文档风格的账号全局响应：本群待处理 1 条 + 本群已处理 1 条 +
# 别的群 1 条 + 归属缺失 1 条；invited 类应整体无视。
_GOCQ_RESPONSE = {
    "invited_requests": [
        {
            "request_id": 11111,
            "invitor_uin": 777,
            "invitor_nick": "路人甲",
            "group_id": 100,
            "group_name": "测试群",
            "checked": False,
            "actor": 0,
        }
    ],
    "join_requests": [
        {
            "request_id": 22222,
            "requester_uin": 456,
            "requester_nick": "小明",
            "message": "同学推荐来的",
            "group_id": 100,
            "group_name": "测试群",
            "checked": False,
            "actor": 0,
        },
        {
            "request_id": 33333,
            "requester_uin": 457,
            "requester_nick": "小红",
            "message": "已经处理过了",
            "group_id": 100,
            "group_name": "测试群",
            "checked": True,
            "actor": 999,
        },
        {
            "request_id": 44444,
            "requester_uin": 458,
            "requester_nick": "别群的人",
            "message": "我申请的是另一个群",
            "group_id": 200,
            "group_name": "别的群",
            "checked": False,
            "actor": 0,
        },
        {
            "request_id": 55555,
            "requester_uin": 459,
            "requester_nick": "无归属",
            "message": "缺 group_id 的脏条目",
            "checked": False,
            "actor": 0,
        },
    ],
}


class GetPendingJoinRequestsToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        bot_registry.clear()

    def tearDown(self) -> None:
        bot_registry.clear()

    async def test_happy_path_filters_to_current_group(self) -> None:
        bot = _StubBot(response=_GOCQ_RESPONSE)
        bot_registry.register(bot)
        outcome = await GetPendingJoinRequestsTool().run({}, **_OK_CTX)
        self.assertTrue(outcome.ok)
        self.assertEqual(len(bot.calls), 1)
        self.assertEqual(bot.calls[0][0], "get_group_system_msg")
        result = outcome.result
        self.assertEqual(result["group_id"], 100)
        self.assertEqual(result["pending_count"], 1)
        self.assertEqual(
            result["requests"],
            [{"user_id": 456, "nickname": "小明", "comment": "同学推荐来的"}],
        )
        self.assertEqual(result["handled_recent_count"], 1)
        self.assertTrue(result["may_be_incomplete"])

    async def test_flag_and_request_id_never_leak(self) -> None:
        bot_registry.register(_StubBot(response=_GOCQ_RESPONSE))
        outcome = await GetPendingJoinRequestsTool().run({}, **_OK_CTX)
        self.assertTrue(outcome.ok)
        dumped = json.dumps(outcome.result, ensure_ascii=False)
        self.assertNotIn("request_id", dumped)
        self.assertNotIn("flag", dumped)
        self.assertNotIn("22222", dumped)  # 待处理条目的凭证值本身也不能漏

    async def test_napcat_variant_keys_and_string_values(self) -> None:
        # NapCat 驼峰列表键 + 字符串化的数值/布尔 + user_id/nickname 备选字段名。
        bot_registry.register(
            _StubBot(
                response={
                    "InvitedRequest": [],
                    "JoinRequest": [
                        {
                            "request_id": "66666",
                            "user_id": "789",
                            "nickname": "阿黄",
                            "comment": "求进",
                            "group_id": "100",
                            "checked": "false",
                        }
                    ],
                }
            )
        )
        outcome = await GetPendingJoinRequestsTool().run({}, **_OK_CTX)
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.result["pending_count"], 1)
        self.assertEqual(
            outcome.result["requests"],
            [{"user_id": 789, "nickname": "阿黄", "comment": "求进"}],
        )

    async def test_null_lists_mean_no_requests(self) -> None:
        # 没有申请时 NapCat 可能给 null 而不是空数组——按空处理，不报错。
        bot_registry.register(
            _StubBot(response={"invited_requests": None, "join_requests": None})
        )
        outcome = await GetPendingJoinRequestsTool().run({}, **_OK_CTX)
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.result["pending_count"], 0)
        self.assertEqual(outcome.result["requests"], [])
        self.assertEqual(outcome.result["handled_recent_count"], 0)

    async def test_unrecognized_payload_is_upstream_payload_invalid(self) -> None:
        bot_registry.register(_StubBot(response={"foo": []}))
        outcome = await GetPendingJoinRequestsTool().run({}, **_OK_CTX)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "upstream_payload_invalid")
        self.assertEqual(outcome.extra["received_keys"], ["foo"])

    async def test_non_dict_payload_is_upstream_payload_invalid(self) -> None:
        bot_registry.register(_StubBot(response=[1, 2, 3]))
        outcome = await GetPendingJoinRequestsTool().run({}, **_OK_CTX)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "upstream_payload_invalid")

    async def test_non_group_scope_returns_tool_unavailable(self) -> None:
        bot_registry.register(_StubBot(response=_GOCQ_RESPONSE))
        outcome = await GetPendingJoinRequestsTool().run(
            {}, scope_key="system", bot_role="admin"
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "tool_unavailable_in_scope")

    async def test_bot_not_admin_is_permission_denied(self) -> None:
        # stub bot 无 get_group_member_info → 实时查失败回退快照 member → 拒。
        bot_registry.register(_StubBot(response=_GOCQ_RESPONSE))
        outcome = await GetPendingJoinRequestsTool().run(
            {}, scope_key="group:100", bot_role="member"
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "permission_denied_bot_role")
        self.assertEqual(outcome.extra["required_bot_role"], "admin")

    async def test_no_bot_available(self) -> None:
        # 无 bot 时角色实时查与快照回退都过不了闸？——快照给 admin 放行门禁，
        # 但 execute 内 get_bot() 拿不到实例 → no_bot_available。
        bot_registry.clear()
        outcome = await GetPendingJoinRequestsTool().run({}, **_OK_CTX)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "no_bot_available")

    async def test_napcat_failure_is_upstream_action_failed(self) -> None:
        bot_registry.register(
            _StubBot(raise_exc=_FakeActionFailed(1200, "系统消息拉取失败"))
        )
        outcome = await GetPendingJoinRequestsTool().run({}, **_OK_CTX)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "upstream_action_failed")
        self.assertEqual(outcome.extra["retcode"], 1200)
        self.assertEqual(outcome.extra["action"], "get_group_system_msg")
        self.assertIn("系统消息拉取失败", outcome.error_message)

    def test_metadata(self) -> None:
        self.assertEqual(
            GetPendingJoinRequestsTool.name, "get_pending_join_requests"
        )
        self.assertEqual(GetPendingJoinRequestsTool.allowed_scopes, ("group",))
        self.assertEqual(GetPendingJoinRequestsTool.required_bot_role, "admin")
        # 只读查询：发起人 tier 沿用 BaseTool 默认 GUEST。
        self.assertEqual(
            GetPendingJoinRequestsTool.required_permission, PermissionTier.GUEST
        )

    def test_usage_md_loaded(self) -> None:
        self.assertIn(
            "get_group_system_msg", GetPendingJoinRequestsTool.usage_prompt
        )
        # 关键指引：查询不给 event_id，审批要回 timeline 对行。
        self.assertIn(
            "respond_to_group_join_request",
            GetPendingJoinRequestsTool.usage_prompt,
        )


if __name__ == "__main__":
    unittest.main()

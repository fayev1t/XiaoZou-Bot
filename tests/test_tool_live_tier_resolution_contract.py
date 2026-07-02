"""Contract test：发起人 tier 判定用**实时**群角色（napcat get_group_member_info,
no_cache），而非事件里记录的 sender.role 快照。

覆盖 BaseTool.enforce_permission → _resolve_triggering_tier 的生产路径（context 不
预置 triggered_by_user_tier，逼它现场解析）：
- 实时角色 admin → 通过 ADMIN 工具（kick），且确实发了 no_cache=True 的实时查询；
- 实时角色 member → permission_denied_user_tier（就算发消息时是 admin 也拦，语义=执行时角色）；
- 发起人是 SUPERUSER → SYSTEM_ADMIN，**跳过**实时查询（cross-cutting）。

用假 session（返回带 user_id 的事件行）+ stub Bot（get_group_member_info 返回指定
role）驱动，不打真 DB / napcat。
"""

from __future__ import annotations

import unittest
from typing import Any

from qqbot.services.agent_loop import bot_registry
from qqbot.services.agent_loop.tools.kick import KickTool


class _FakeRow:
    def __init__(self, user_id: Any, payload: dict | None = None) -> None:
        self.user_id = user_id
        self.payload = payload or {}


class _FakeResult:
    def __init__(self, row: Any) -> None:
        self._row = row

    def scalar_one_or_none(self) -> Any:
        return self._row


class _FakeSession:
    def __init__(self, row: Any) -> None:
        self._row = row

    async def execute(self, stmt: Any) -> _FakeResult:
        return _FakeResult(self._row)

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None


def _session_factory(row: Any):
    def factory() -> _FakeSession:
        return _FakeSession(row)

    return factory


class _StubBot:
    """get_group_member_info **按 user_id** 返回角色，记录查询以校验 no_cache。

    kick 现在会实时查三类角色（发起人 tier、bot 自身角色、踢的目标角色），所以桩
    必须区分 user：
    - 发起人 `initiator_id` → 指定的实时群角色（本测试的被测对象）；
    - bot 自己（self_id="1"）→ owner（过 `enforce_bot_admin` 的**实时 bot 角色**查询）；
    - 踢的目标（5）→ member（过 kick 的层级前置判定：owner > member）。
    """

    def __init__(self, live_role: str, initiator_id: int) -> None:
        self.self_id = "1"
        self._live_role = live_role
        self._initiator_id = str(initiator_id)
        self.member_queries: list[dict] = []
        self.kicks: list[dict] = []

    async def get_group_member_info(self, **kwargs: Any) -> dict:
        self.member_queries.append(kwargs)
        uid = str(kwargs.get("user_id"))
        if uid == self.self_id:
            role = "owner"  # bot 自己：群主 → 过 enforce_bot_admin
        elif uid == self._initiator_id:
            role = self._live_role  # 发起人实时角色（被测）
        else:
            role = "member"  # 踢的目标：普通成员 → 过层级前置判定
        return {"role": role, "user_id": kwargs.get("user_id")}

    async def set_group_kick(self, **kwargs: Any) -> dict:
        self.kicks.append(kwargs)
        return {}


class LiveTierResolutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        bot_registry.clear()

    def tearDown(self) -> None:
        bot_registry.clear()

    async def _run_kick(self, *, live_role: str, su: frozenset[str], user_id: int):
        bot = _StubBot(live_role, initiator_id=user_id)
        bot_registry.register(bot)
        outcome = await KickTool().run(
            {"user_id": 5},
            scope_key="group:100",
            triggered_by_event_id="E1",  # 不预置 tier → 逼实时解析
            session_factory=_session_factory(_FakeRow(user_id=user_id)),
            superusers=su,
            bot_role="owner",  # bot 自身群主 → 过 enforce_bot_admin
        )
        return outcome, bot

    async def test_live_admin_passes_admin_tool(self) -> None:
        outcome, bot = await self._run_kick(
            live_role="admin", su=frozenset(), user_id=777
        )
        # 实时查到发起人现在是 admin → 过 tier 门（kick 要 ADMIN）→ 执行踢人
        self.assertTrue(outcome.ok, outcome)
        self.assertEqual(bot.kicks[0]["user_id"], 5)
        # 发起人（777）确实被实时查询、且 no_cache=True（取最新、不吃缓存）。
        # 现在还会另查 bot 自己(1)与目标(5)的角色，故按 user_id 过滤而非数总量。
        init_q = [q for q in bot.member_queries if str(q["user_id"]) == "777"]
        self.assertEqual(len(init_q), 1)
        self.assertEqual(init_q[0]["group_id"], 100)
        self.assertTrue(init_q[0]["no_cache"])

    async def test_live_member_denied_admin_tool(self) -> None:
        # 就算发消息时是 admin，实时已是 member → 拦（语义=执行时角色）。
        outcome, bot = await self._run_kick(
            live_role="member", su=frozenset(), user_id=777
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "permission_denied_user_tier")
        self.assertEqual(bot.kicks, [])  # 没执行踢人
        # 发起人确实实时查过（member → 拦在 tier 门，enforce_permission 先于 bot 角色查询，
        # 故此路径只查了发起人这一次）。
        init_q = [q for q in bot.member_queries if str(q["user_id"]) == "777"]
        self.assertEqual(len(init_q), 1)

    async def test_superuser_skips_live_query(self) -> None:
        # 发起人在 SUPERUSERS → SYSTEM_ADMIN，越过群角色、**不**实时查。
        outcome, bot = await self._run_kick(
            live_role="member",  # 即便群角色是 member，也不该被查/被拦
            su=frozenset({"999"}),
            user_id=999,
        )
        self.assertTrue(outcome.ok, outcome)
        # 发起人（999，SUPERUSER）**未**被实时查询（cross-cutting 跳过群角色解析）。
        # bot 自己(1)与目标(5)仍会被查——那是 bot 角色 / 层级判定，不是发起人 tier。
        self.assertNotIn("999", [str(q["user_id"]) for q in bot.member_queries])
        self.assertEqual(bot.kicks[0]["user_id"], 5)


if __name__ == "__main__":
    unittest.main()

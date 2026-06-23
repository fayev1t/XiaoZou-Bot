"""Contract tests for bot_role_sweep + v2_main 反射接线。

覆盖：
- sweep_bot_role:
    * get_group_list 失败 → 不抛、返回 0
    * group_list 含两个群 → 写两条 runtime.bot_role_observed
    * 单个群 get_group_member_info 失败 → 跳过、不阻塞其他群
    * role 非法值 → 跳过
    * self_id 空 → 直接返回 0
- observe_bot_role_change:
    * 写一条事件
    * 异常被吞掉（不抛出）
- v2_main 反射逻辑：
    * notice.group_admin user_id==self_id sub=set → role=admin
    * notice.group_admin user_id==self_id sub=unset → role=member
    * notice.group_admin user_id!=self_id → 不写
    * notice.group_increase user_id==self_id → role=member (member 兜底)
    * notice.group_decrease user_id==self_id → role=member
    * meta lifecycle connect → 触发 sweep schedule
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from typing import Any
from unittest import mock

from qqbot.services.agent_loop.bot_role_sweep import (
    observe_bot_role_change,
    reflect_bot_role_from_meta,
    reflect_bot_role_from_notice,
    sweep_bot_role,
)


class _RecordingSession:
    def __init__(self, store: list[Any]) -> None:
        self._store = store

    async def execute(self, stmt: Any) -> Any:
        self._store.append(stmt)
        return SimpleNamespace(rowcount=1)

    async def commit(self) -> None:
        return None

    async def __aenter__(self) -> "_RecordingSession":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None


def _factory_for(store: list[Any]):
    def f() -> _RecordingSession:
        return _RecordingSession(store)

    return f


def _params_by_index(captured: list[Any]) -> list[dict]:
    return [stmt.compile().params for stmt in captured]


def _payloads_with_type(captured: list[Any], event_type: str) -> list[dict]:
    return [
        p.get("payload") or {}
        for p in _params_by_index(captured)
        if p.get("type") == event_type
    ]


# ── stub bot ──


class _StubBot:
    def __init__(
        self,
        *,
        self_id: str | int = "9000",
        group_list: Any = None,
        member_info_by_group: dict[int, Any] | None = None,
        list_raises: Exception | None = None,
        info_raises_for: set[int] | None = None,
    ) -> None:
        self.self_id = self_id
        self._group_list = group_list
        self._member_info = member_info_by_group or {}
        self._list_raises = list_raises
        self._info_raises_for = info_raises_for or set()
        self.calls: list[tuple[str, dict]] = []

    async def call_api(self, api: str, **kwargs: Any) -> Any:
        self.calls.append((api, kwargs))
        if api == "get_group_list":
            if self._list_raises:
                raise self._list_raises
            return self._group_list
        if api == "get_group_member_info":
            gid = kwargs.get("group_id")
            if gid in self._info_raises_for:
                raise RuntimeError(f"info failed for {gid}")
            return self._member_info.get(gid)
        raise AssertionError(f"unexpected api: {api}")


class SweepBotRoleContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_empty_self_id_returns_zero(self) -> None:
        bot = _StubBot(self_id="", group_list=[])
        store: list[Any] = []
        written = await sweep_bot_role(bot, _factory_for(store))
        self.assertEqual(written, 0)
        self.assertEqual(store, [])

    async def test_get_group_list_failure_returns_zero(self) -> None:
        bot = _StubBot(list_raises=RuntimeError("boom"))
        store: list[Any] = []
        written = await sweep_bot_role(bot, _factory_for(store))
        self.assertEqual(written, 0)
        self.assertEqual(store, [])

    async def test_writes_one_per_group(self) -> None:
        bot = _StubBot(
            self_id="9000",
            group_list=[{"group_id": 100}, {"group_id": 200}],
            member_info_by_group={
                100: {"role": "admin"},
                200: {"role": "owner"},
            },
        )
        store: list[Any] = []
        written = await sweep_bot_role(bot, _factory_for(store))
        self.assertEqual(written, 2)
        payloads = _payloads_with_type(store, "runtime.bot_role_observed")
        roles_by_group = {p["group_id"]: p["role"] for p in payloads}
        self.assertEqual(roles_by_group, {100: "admin", 200: "owner"})
        # 每条都打了 source 标
        self.assertEqual({p["source"] for p in payloads}, {"lifecycle_sweep"})
        self.assertEqual({p["self_id"] for p in payloads}, {"9000"})

    async def test_partial_failure_does_not_block_others(self) -> None:
        bot = _StubBot(
            self_id="9000",
            group_list=[{"group_id": 100}, {"group_id": 200}],
            member_info_by_group={
                100: {"role": "admin"},
                200: {"role": "member"},
            },
            info_raises_for={100},
        )
        store: list[Any] = []
        written = await sweep_bot_role(bot, _factory_for(store))
        self.assertEqual(written, 1)
        payloads = _payloads_with_type(store, "runtime.bot_role_observed")
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["group_id"], 200)

    async def test_invalid_role_skipped(self) -> None:
        bot = _StubBot(
            self_id="9000",
            group_list=[{"group_id": 100}, {"group_id": 200}],
            member_info_by_group={
                100: {"role": "what??"},  # 非法 role
                200: {"role": "owner"},
            },
        )
        store: list[Any] = []
        written = await sweep_bot_role(bot, _factory_for(store))
        self.assertEqual(written, 1)
        payloads = _payloads_with_type(store, "runtime.bot_role_observed")
        self.assertEqual(payloads[0]["group_id"], 200)


class ObserveBotRoleChangeContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_writes_event(self) -> None:
        store: list[Any] = []
        await observe_bot_role_change(
            session_factory=_factory_for(store),
            group_id=100,
            self_id="9000",
            role="admin",
            source="group_admin_notice",
        )
        payloads = _payloads_with_type(store, "runtime.bot_role_observed")
        self.assertEqual(len(payloads), 1)
        p = payloads[0]
        self.assertEqual(p["role"], "admin")
        self.assertEqual(p["group_id"], 100)
        self.assertEqual(p["self_id"], "9000")
        self.assertEqual(p["source"], "group_admin_notice")

    async def test_swallows_exceptions(self) -> None:
        """写失败也不应该抛 —— 反射失败不能影响主消息处理。"""

        def bad_factory() -> Any:
            raise RuntimeError("DB down")

        # 没有 raises = pass
        await observe_bot_role_change(
            session_factory=bad_factory,
            group_id=100,
            self_id="9000",
            role="admin",
            source="group_admin_notice",
        )


# ── 反射逻辑 ──


_REFLECT_MODULE = "qqbot.services.agent_loop.bot_role_sweep"


class ReflectFromNoticeContractTest(unittest.IsolatedAsyncioTestCase):
    """reflect_bot_role_from_notice 是 v2_main notice handler 的核心。
    不依赖 nonebot 初始化，可直接对 SimpleNamespace 事件做 dispatch。
    """

    async def test_group_admin_set_writes_admin(self) -> None:
        bot = SimpleNamespace(self_id=9000)
        event = SimpleNamespace(
            notice_type="group_admin",
            sub_type="set",
            user_id=9000,
            group_id=100,
        )
        with mock.patch(
            f"{_REFLECT_MODULE}.observe_bot_role_change",
            new=mock.AsyncMock(),
        ) as patched:
            await reflect_bot_role_from_notice(bot, event, _factory_for([]))
            patched.assert_awaited_once()
            kwargs = patched.call_args.kwargs
            self.assertEqual(kwargs["role"], "admin")
            self.assertEqual(kwargs["group_id"], 100)
            self.assertEqual(kwargs["self_id"], "9000")
            self.assertEqual(kwargs["source"], "group_admin_notice")

    async def test_group_admin_unset_writes_member(self) -> None:
        bot = SimpleNamespace(self_id=9000)
        event = SimpleNamespace(
            notice_type="group_admin",
            sub_type="unset",
            user_id=9000,
            group_id=100,
        )
        with mock.patch(
            f"{_REFLECT_MODULE}.observe_bot_role_change",
            new=mock.AsyncMock(),
        ) as patched:
            await reflect_bot_role_from_notice(bot, event, _factory_for([]))
            self.assertEqual(patched.call_args.kwargs["role"], "member")

    async def test_group_admin_other_user_ignored(self) -> None:
        bot = SimpleNamespace(self_id=9000)
        event = SimpleNamespace(
            notice_type="group_admin",
            sub_type="set",
            user_id=12345,  # 不是 bot
            group_id=100,
        )
        with mock.patch(
            f"{_REFLECT_MODULE}.observe_bot_role_change",
            new=mock.AsyncMock(),
        ) as patched:
            await reflect_bot_role_from_notice(bot, event, _factory_for([]))
            patched.assert_not_awaited()

    async def test_group_increase_for_self_writes_member(self) -> None:
        bot = SimpleNamespace(self_id=9000)
        event = SimpleNamespace(
            notice_type="group_increase",
            sub_type=None,
            user_id=9000,
            group_id=300,
        )
        with mock.patch(
            f"{_REFLECT_MODULE}.observe_bot_role_change",
            new=mock.AsyncMock(),
        ) as patched:
            await reflect_bot_role_from_notice(bot, event, _factory_for([]))
            kwargs = patched.call_args.kwargs
            self.assertEqual(kwargs["role"], "member")
            self.assertEqual(kwargs["source"], "group_increase_notice")

    async def test_group_decrease_for_self_writes_member(self) -> None:
        bot = SimpleNamespace(self_id=9000)
        event = SimpleNamespace(
            notice_type="group_decrease",
            sub_type=None,
            user_id=9000,
            group_id=300,
        )
        with mock.patch(
            f"{_REFLECT_MODULE}.observe_bot_role_change",
            new=mock.AsyncMock(),
        ) as patched:
            await reflect_bot_role_from_notice(bot, event, _factory_for([]))
            self.assertEqual(
                patched.call_args.kwargs["source"], "group_decrease_notice"
            )

    async def test_unrelated_notice_type_ignored(self) -> None:
        bot = SimpleNamespace(self_id=9000)
        event = SimpleNamespace(
            notice_type="group_recall", user_id=9000, group_id=100
        )
        with mock.patch(
            f"{_REFLECT_MODULE}.observe_bot_role_change",
            new=mock.AsyncMock(),
        ) as patched:
            await reflect_bot_role_from_notice(bot, event, _factory_for([]))
            patched.assert_not_awaited()


class ReflectFromMetaContractTest(unittest.TestCase):
    def test_lifecycle_connect_schedules_sweep(self) -> None:
        bot = SimpleNamespace(self_id=9000)
        event = SimpleNamespace(meta_event_type="lifecycle", sub_type="connect")
        sf = _factory_for([])
        with mock.patch(f"{_REFLECT_MODULE}.schedule_sweep") as patched:
            reflect_bot_role_from_meta(bot, event, sf)
            patched.assert_called_once_with(bot, sf)

    def test_lifecycle_disable_does_not_schedule_sweep(self) -> None:
        bot = SimpleNamespace(self_id=9000)
        event = SimpleNamespace(meta_event_type="lifecycle", sub_type="disable")
        with mock.patch(f"{_REFLECT_MODULE}.schedule_sweep") as patched:
            reflect_bot_role_from_meta(bot, event, _factory_for([]))
            patched.assert_not_called()

    def test_heartbeat_does_not_schedule_sweep(self) -> None:
        bot = SimpleNamespace(self_id=9000)
        event = SimpleNamespace(meta_event_type="heartbeat", sub_type="")
        with mock.patch(f"{_REFLECT_MODULE}.schedule_sweep") as patched:
            reflect_bot_role_from_meta(bot, event, _factory_for([]))
            patched.assert_not_called()


class V2MainTextContractTest(unittest.TestCase):
    """text-based 校验 v2_main 真的接上了 reflect_bot_role_*：万一 import
    挪窝或 wiring 丢了，让测试网住。"""

    def setUp(self) -> None:
        from pathlib import Path

        self.text = (
            Path(__file__).resolve().parents[1]
            / "qqbot"
            / "plugins"
            / "v2_main.py"
        ).read_text(encoding="utf-8")

    def test_notice_handler_calls_reflect(self) -> None:
        self.assertIn(
            "reflect_bot_role_from_notice(bot, event, AsyncSessionLocal)",
            self.text,
        )

    def test_meta_handler_calls_reflect(self) -> None:
        self.assertIn(
            "reflect_bot_role_from_meta(bot, event, AsyncSessionLocal)",
            self.text,
        )

    def test_imports_reflect_helpers(self) -> None:
        self.assertIn("reflect_bot_role_from_meta", self.text)
        self.assertIn("reflect_bot_role_from_notice", self.text)


if __name__ == "__main__":
    unittest.main()

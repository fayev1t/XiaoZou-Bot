"""Contract tests for request_auto_approval（好友申请 / 邀请入群自动同意）。

链路（2026-07-03 拆分）：EventIngest 把这两类申请以 runtime_only 落库后，
v2_main 的 request handler 调 maybe_auto_approve —— 只在 status=="inserted"
且 type 命中时回执 napcat，并补 runtime.request_auto_handled 审计事件。

Covers:
- friend → set_friend_add_request(flag, approve=True) + 审计事件（因果链沿用申请事件）
- group.invite → set_group_add_request(flag, sub_type="invite", approve=True)
- 不动作路径：result=None / duplicate / group.add / 非 request 事件
- napcat 报错：不 raise，审计事件 ok=false 且带 error
- flag 缺失：不调 napcat，审计事件 ok=false
- 审计写库失败：不 raise（回执已完成，只丢审计 + 日志）

DB 用 _RecordingSession 捕获 insert 语句（不打真库），napcat 用 stub bot。
"""

from __future__ import annotations

import unittest
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from qqbot.services.event_ingest import IngestResult, SystemEvent
from qqbot.services.request_auto_approval import maybe_auto_approve

SHANGHAI = ZoneInfo("Asia/Shanghai")
BASE_TIME = datetime(2026, 7, 3, 15, 0, 0, tzinfo=SHANGHAI)


def _request_event(
    *,
    type: str,
    payload: dict,
    event_id: str = "REQ1",
    scope: str = "system",
    group_id: int | None = None,
) -> SystemEvent:
    return SystemEvent(
        event_id=event_id,
        occurred_at=BASE_TIME,
        origin="external",
        type=type,
        scope=scope,
        group_id=group_id,
        user_id=payload.get("user_id"),
        visibility="runtime_only",
        correlation_id=event_id,  # 外部事件自相关
        causation_id=None,
        idempotency_key=f"10001:request:x:{event_id}",
        payload=payload,
        raw=None,
    )


def _friend_result(status: str = "inserted") -> IngestResult:
    return IngestResult(
        status=status,  # type: ignore[arg-type]
        event=_request_event(
            type="external.request.friend",
            payload={"user_id": 222, "comment": "hi", "flag": "FFLAG"},
        ),
    )


def _invite_result() -> IngestResult:
    return IngestResult(
        status="inserted",
        event=_request_event(
            type="external.request.group.invite",
            payload={
                "sub_type": "invite",
                "group_id": 100,
                "user_id": 222,
                "flag": "IFLAG",
            },
        ),
    )


class _StubBot:
    def __init__(self, raise_exc: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._raise = raise_exc

    async def set_friend_add_request(self, **kwargs: Any) -> dict:
        self.calls.append(("set_friend_add_request", kwargs))
        if self._raise is not None:
            raise self._raise
        return {}

    async def set_group_add_request(self, **kwargs: Any) -> dict:
        self.calls.append(("set_group_add_request", kwargs))
        if self._raise is not None:
            raise self._raise
        return {}


class _RecordingSession:
    """async session double：捕获执行的 insert 语句（write_internal_event →
    persist_event 用 pg_insert）。"""

    def __init__(self, store: list[Any], raise_exc: Exception | None = None) -> None:
        self._store = store
        self._raise = raise_exc

    async def execute(self, stmt: Any) -> Any:
        if self._raise is not None:
            raise self._raise
        self._store.append(stmt)
        from types import SimpleNamespace

        return SimpleNamespace(rowcount=1)

    async def commit(self) -> None:
        return None

    async def __aenter__(self) -> "_RecordingSession":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None


def _factory_for(store: list[Any], raise_exc: Exception | None = None):
    def factory() -> _RecordingSession:
        return _RecordingSession(store, raise_exc)

    return factory


def _inserted_params(stmt: Any) -> dict:
    """从 pg_insert 语句抽出 values 的具名参数（与 test_wait_tool_contract 同法）。"""
    return {k: v for k, v in stmt.compile().params.items()}


class AutoApproveHappyPathTests(unittest.IsolatedAsyncioTestCase):
    async def test_friend_request_approved_and_audited(self) -> None:
        bot = _StubBot()
        store: list[Any] = []
        ok = await maybe_auto_approve(bot, _friend_result(), _factory_for(store))
        self.assertTrue(ok)
        # napcat 回执
        self.assertEqual(len(bot.calls), 1)
        method, kwargs = bot.calls[0]
        self.assertEqual(method, "set_friend_add_request")
        self.assertEqual(kwargs["flag"], "FFLAG")
        self.assertTrue(kwargs["approve"])
        # 审计事件：runtime.request_auto_handled，因果链沿用申请事件
        self.assertEqual(len(store), 1)
        params = _inserted_params(store[0])
        self.assertEqual(params["type"], "runtime.request_auto_handled")
        self.assertEqual(params["origin"], "runtime")
        self.assertEqual(params["scope"], "system")
        self.assertEqual(params["visibility"], "runtime_only")
        self.assertEqual(params["correlation_id"], "REQ1")
        self.assertEqual(params["causation_id"], "REQ1")
        payload = params["payload"]
        self.assertEqual(payload["request_event_id"], "REQ1")
        self.assertEqual(payload["request_type"], "friend")
        self.assertEqual(payload["user_id"], 222)
        self.assertTrue(payload["approve"])
        self.assertTrue(payload["ok"])
        self.assertNotIn("error", payload)

    async def test_group_invite_approved_with_sub_type(self) -> None:
        bot = _StubBot()
        store: list[Any] = []
        ok = await maybe_auto_approve(bot, _invite_result(), _factory_for(store))
        self.assertTrue(ok)
        method, kwargs = bot.calls[0]
        self.assertEqual(method, "set_group_add_request")
        self.assertEqual(kwargs["flag"], "IFLAG")
        self.assertEqual(kwargs["sub_type"], "invite")
        self.assertTrue(kwargs["approve"])
        payload = _inserted_params(store[0])["payload"]
        self.assertEqual(payload["request_type"], "group_invite")
        self.assertEqual(payload["group_id"], 100)


class AutoApproveNoActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_none_result_no_action(self) -> None:
        bot = _StubBot()
        store: list[Any] = []
        ok = await maybe_auto_approve(bot, None, _factory_for(store))
        self.assertFalse(ok)
        self.assertEqual(bot.calls, [])
        self.assertEqual(store, [])

    async def test_duplicate_not_reapproved(self) -> None:
        # napcat 重推同 flag → persist 层判 duplicate → 不得二次审批。
        bot = _StubBot()
        store: list[Any] = []
        ok = await maybe_auto_approve(
            bot, _friend_result(status="duplicate"), _factory_for(store)
        )
        self.assertFalse(ok)
        self.assertEqual(bot.calls, [])
        self.assertEqual(store, [])

    async def test_group_add_not_touched(self) -> None:
        # 入群申请走群内 LLM 授权链路，自动审批不得碰它。
        bot = _StubBot()
        store: list[Any] = []
        result = IngestResult(
            status="inserted",
            event=_request_event(
                type="external.request.group.add",
                payload={
                    "sub_type": "add",
                    "group_id": 100,
                    "user_id": 222,
                    "flag": "GFLAG",
                },
                scope="group",
                group_id=100,
            ),
        )
        ok = await maybe_auto_approve(bot, result, _factory_for(store))
        self.assertFalse(ok)
        self.assertEqual(bot.calls, [])
        self.assertEqual(store, [])

    async def test_non_request_event_no_action(self) -> None:
        bot = _StubBot()
        store: list[Any] = []
        result = IngestResult(
            status="inserted",
            event=_request_event(
                type="external.message.group.normal",
                payload={"flag": "x"},
            ),
        )
        ok = await maybe_auto_approve(bot, result, _factory_for(store))
        self.assertFalse(ok)
        self.assertEqual(bot.calls, [])
        self.assertEqual(store, [])


class AutoApproveFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_napcat_error_audited_not_raised(self) -> None:
        bot = _StubBot(raise_exc=RuntimeError("flag 已失效"))
        store: list[Any] = []
        ok = await maybe_auto_approve(bot, _friend_result(), _factory_for(store))
        self.assertFalse(ok)
        # 审计事件仍要写：ok=false + error
        payload = _inserted_params(store[0])["payload"]
        self.assertFalse(payload["ok"])
        self.assertIn("flag 已失效", payload["error"])

    async def test_missing_flag_no_call_but_audited(self) -> None:
        bot = _StubBot()
        store: list[Any] = []
        result = IngestResult(
            status="inserted",
            event=_request_event(
                type="external.request.friend",
                payload={"user_id": 222},  # 无 flag
            ),
        )
        ok = await maybe_auto_approve(bot, result, _factory_for(store))
        self.assertFalse(ok)
        self.assertEqual(bot.calls, [])
        payload = _inserted_params(store[0])["payload"]
        self.assertFalse(payload["ok"])
        self.assertIn("flag", payload["error"])

    async def test_no_bot_audited(self) -> None:
        store: list[Any] = []
        ok = await maybe_auto_approve(None, _friend_result(), _factory_for(store))
        self.assertFalse(ok)
        payload = _inserted_params(store[0])["payload"]
        self.assertFalse(payload["ok"])

    async def test_audit_write_failure_swallowed(self) -> None:
        # 回执成功后审计写库炸了：不 raise（丢审计留日志），返回值仍反映回执成功。
        bot = _StubBot()
        store: list[Any] = []
        ok = await maybe_auto_approve(
            bot,
            _friend_result(),
            _factory_for(store, raise_exc=RuntimeError("db down")),
        )
        self.assertTrue(ok)
        self.assertEqual(len(bot.calls), 1)


if __name__ == "__main__":
    unittest.main()

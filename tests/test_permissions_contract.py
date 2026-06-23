"""Contract tests for qqbot.core.permissions.

覆盖：
- PermissionTier IntEnum 排序
- load_superusers 解析 SUPERUSERS env（JSON list / 缺失 / 损坏）
- tier_from_group_role 映射
- resolve_user_tier_from_event:
    * event_id=None → GUEST
    * 用户命中 SUPERUSERS → SYSTEM_ADMIN（覆盖群 owner）
    * sender.role=owner|admin|member → OWNER|ADMIN|GUEST
    * payload 缺 sender → GUEST
    * DB 找不到 event → GUEST
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any
from unittest import mock

from qqbot.core.permissions import (
    PermissionTier,
    load_superusers,
    resolve_user_tier_from_event,
    tier_from_group_role,
)

# 直接 patch get_env_value 而不是 os.environ —— settings 还会从 .env 文件回退读，
# 单纯清 os.environ 在本仓库无效。
_ENV_PATCH_TARGET = "qqbot.core.permissions.get_env_value"


class _FakeRowSession:
    """走 SQLAlchemy ``select().where()`` 路径，按 event_id 返回伪行。"""

    def __init__(self, by_event_id: dict[str, Any]) -> None:
        self._by_event_id = by_event_id

    async def execute(self, stmt: Any) -> Any:
        # 取 select 出来的 WHERE event_id == X 中的 X
        params = stmt.compile().params
        wanted = params.get("event_id_1") or params.get("event_id")
        row = self._by_event_id.get(wanted)
        return SimpleNamespace(scalar_one_or_none=lambda: row)

    async def __aenter__(self) -> "_FakeRowSession":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None


def _factory(rows: dict[str, Any]):
    def f() -> _FakeRowSession:
        return _FakeRowSession(rows)

    return f


def _row(event_id: str, user_id: int | None, role: str | None) -> Any:
    payload: dict = {}
    if role is not None:
        payload["sender"] = {"user_id": user_id, "role": role}
    return SimpleNamespace(event_id=event_id, user_id=user_id, payload=payload)


class PermissionTierContractTest(unittest.TestCase):
    def test_int_ordering(self) -> None:
        self.assertLess(PermissionTier.GUEST, PermissionTier.ADMIN)
        self.assertLess(PermissionTier.ADMIN, PermissionTier.OWNER)
        self.assertLess(PermissionTier.OWNER, PermissionTier.SYSTEM_ADMIN)
        self.assertEqual(PermissionTier.GUEST.value, 10)
        self.assertEqual(PermissionTier.SYSTEM_ADMIN.value, 40)

    def test_tier_from_group_role(self) -> None:
        self.assertEqual(tier_from_group_role("owner"), PermissionTier.OWNER)
        self.assertEqual(tier_from_group_role("admin"), PermissionTier.ADMIN)
        self.assertEqual(tier_from_group_role("member"), PermissionTier.GUEST)
        self.assertEqual(tier_from_group_role(None), PermissionTier.GUEST)
        self.assertEqual(tier_from_group_role(""), PermissionTier.GUEST)
        self.assertEqual(tier_from_group_role("nonsense"), PermissionTier.GUEST)
        # 大小写 / 前后空白容错
        self.assertEqual(tier_from_group_role(" Admin "), PermissionTier.ADMIN)


class LoadSuperusersContractTest(unittest.TestCase):
    def test_parses_json_list(self) -> None:
        with mock.patch(_ENV_PATCH_TARGET, return_value='["111","222"]'):
            self.assertEqual(load_superusers(), frozenset({"111", "222"}))

    def test_missing_env_returns_empty(self) -> None:
        with mock.patch(_ENV_PATCH_TARGET, return_value=None):
            self.assertEqual(load_superusers(), frozenset())

    def test_malformed_returns_empty(self) -> None:
        with mock.patch(_ENV_PATCH_TARGET, return_value="not-json"):
            self.assertEqual(load_superusers(), frozenset())

    def test_non_list_returns_empty(self) -> None:
        with mock.patch(_ENV_PATCH_TARGET, return_value='{"x":1}'):
            self.assertEqual(load_superusers(), frozenset())

    def test_ints_coerced_to_strings(self) -> None:
        with mock.patch(_ENV_PATCH_TARGET, return_value="[111, 222]"):
            self.assertEqual(load_superusers(), frozenset({"111", "222"}))


class ResolveUserTierContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_no_event_id_is_guest(self) -> None:
        tier, uid = await resolve_user_tier_from_event(
            None,
            session_factory=_factory({}),
            superusers=frozenset(),
        )
        self.assertEqual(tier, PermissionTier.GUEST)
        self.assertIsNone(uid)

    async def test_db_miss_is_guest(self) -> None:
        tier, uid = await resolve_user_tier_from_event(
            "E_unknown",
            session_factory=_factory({}),
            superusers=frozenset(),
        )
        self.assertEqual(tier, PermissionTier.GUEST)
        self.assertIsNone(uid)

    async def test_owner_maps_to_owner_tier(self) -> None:
        rows = {"E1": _row("E1", user_id=42, role="owner")}
        tier, uid = await resolve_user_tier_from_event(
            "E1",
            session_factory=_factory(rows),
            superusers=frozenset(),
        )
        self.assertEqual(tier, PermissionTier.OWNER)
        self.assertEqual(uid, "42")

    async def test_admin_maps_to_admin_tier(self) -> None:
        rows = {"E1": _row("E1", user_id=42, role="admin")}
        tier, _ = await resolve_user_tier_from_event(
            "E1",
            session_factory=_factory(rows),
            superusers=frozenset(),
        )
        self.assertEqual(tier, PermissionTier.ADMIN)

    async def test_member_maps_to_guest(self) -> None:
        rows = {"E1": _row("E1", user_id=42, role="member")}
        tier, _ = await resolve_user_tier_from_event(
            "E1",
            session_factory=_factory(rows),
            superusers=frozenset(),
        )
        self.assertEqual(tier, PermissionTier.GUEST)

    async def test_superuser_beats_group_role(self) -> None:
        # 同一个用户在群里只是 member，但他是 SUPERUSER —— SU 永远赢
        rows = {"E1": _row("E1", user_id=999, role="member")}
        tier, uid = await resolve_user_tier_from_event(
            "E1",
            session_factory=_factory(rows),
            superusers=frozenset({"999"}),
        )
        self.assertEqual(tier, PermissionTier.SYSTEM_ADMIN)
        self.assertEqual(uid, "999")

    async def test_payload_missing_sender_is_guest(self) -> None:
        # notice 事件、agent 事件等可能没有 sender 字段；这种触发不应给到 tier
        bare = SimpleNamespace(event_id="E1", user_id=42, payload={})
        tier, uid = await resolve_user_tier_from_event(
            "E1",
            session_factory=_factory({"E1": bare}),
            superusers=frozenset(),
        )
        self.assertEqual(tier, PermissionTier.GUEST)
        self.assertEqual(uid, "42")


if __name__ == "__main__":
    unittest.main()

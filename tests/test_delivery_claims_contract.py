"""Contract tests for delivery_claims.try_claim (worker 投递去重 / 租约).

Covers (② 至少一次重发修复):
- 首次抢锁:INSERT ON CONFLICT DO NOTHING 命中(rowcount>0)→ True,只发一条语句
- 已有未过期租约:INSERT 不中(rowcount=0)→ 再条件 UPDATE 不中(rowcount=0)→ False
- 已有过期租约:INSERT 不中 → 条件 UPDATE 命中 → True
- fail-open:session 抛异常 → 返回 True(绝不因去重失败阻断投递)
- INSERT 语句确实打向 agent_delivery_claims 且带 on_conflict

不打真实 DB:用可编排 rowcount 的 stub session;语句按 postgresql dialect 编译。
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from typing import Any

from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql.dml import Insert as PGInsert
from sqlalchemy.sql.dml import Update

from qqbot.services.agent_loop.delivery_claims import try_claim


class _ScriptedSession:
    """按 execute 调用次序返回预设 rowcount;捕获语句供断言。"""

    def __init__(self, rowcounts: list[int], *, raise_on_enter: bool = False) -> None:
        self._rowcounts = rowcounts
        self._i = 0
        self.captured: list[Any] = []
        self.committed = False
        self._raise_on_enter = raise_on_enter

    async def execute(self, stmt: Any) -> Any:
        self.captured.append(stmt)
        rc = self._rowcounts[self._i] if self._i < len(self._rowcounts) else 0
        self._i += 1
        return SimpleNamespace(rowcount=rc)

    async def commit(self) -> None:
        self.committed = True

    async def __aenter__(self) -> "_ScriptedSession":
        if self._raise_on_enter:
            raise RuntimeError("db down")
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None


def _factory(session: _ScriptedSession):
    def f() -> _ScriptedSession:
        return session

    return f


def _claim(session: _ScriptedSession) -> bool:
    return asyncio.run(try_claim(_factory(session), "EID1", "tool"))


class TryClaimTests(unittest.TestCase):
    def test_first_claim_succeeds_with_single_insert(self) -> None:
        s = _ScriptedSession([1])  # INSERT 命中
        self.assertTrue(_claim(s))
        self.assertEqual(len(s.captured), 1)  # 只发了 INSERT,没走 UPDATE
        self.assertIsInstance(s.captured[0], PGInsert)
        self.assertEqual(s.captured[0].table.name, "agent_delivery_claims")
        self.assertTrue(s.committed)

    def test_valid_lease_held_by_other_returns_false(self) -> None:
        s = _ScriptedSession([0, 0])  # INSERT 冲突 + UPDATE 未命中(租约未过期)
        self.assertFalse(_claim(s))
        self.assertEqual(len(s.captured), 2)
        self.assertIsInstance(s.captured[0], PGInsert)
        self.assertIsInstance(s.captured[1], Update)

    def test_expired_lease_is_reclaimed(self) -> None:
        s = _ScriptedSession([0, 1])  # INSERT 冲突 + UPDATE 命中(租约已过期)
        self.assertTrue(_claim(s))
        self.assertEqual(len(s.captured), 2)
        self.assertTrue(s.committed)

    def test_db_error_fails_open(self) -> None:
        # session 进入即抛 → try_claim 必须放行(返回 True),绝不阻断投递
        s = _ScriptedSession([], raise_on_enter=True)
        self.assertTrue(_claim(s))

    def test_insert_compiles_under_pg_dialect(self) -> None:
        # 确保 ON CONFLICT 在真实 dialect 下可编译(回归:与 persist_event 同构)
        s = _ScriptedSession([1])
        _claim(s)
        compiled = s.captured[0].compile(dialect=postgresql.dialect())
        self.assertIn("ON CONFLICT", str(compiled).upper())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

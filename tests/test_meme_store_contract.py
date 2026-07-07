"""Contract tests for meme_store（agent_memes 表情包收藏读写）。

Covers（表情包工具黑盒设计.md §存储；2026-07-06 起全局共享）：
- 全局共享：所有读写固定用哨兵 scope_key = MEME_SCOPE_GLOBAL（隔离契约
  §9.2 第 6 条例外），insert 落 'global'、get/load 按 'global' 过滤。
- insert_meme：INSERT ... ON CONFLICT (scope_key, file_hash) DO NOTHING；
  rowcount=1 → True（新插入），rowcount=0 → False（已存在，调用方折
  already_saved，**不覆盖**）；values 完整落所有列。
- get_meme：按 hash 单条精确查 → MemeView；未命中 → None。
- load_saved_memes：created_at 倒序 + LIMIT（语句面断言）；UTC 时间
  normalize 到北京时间（与 task_store 同约定）。

不打真实 DB：recording / stub session 捕获语句，postgresql dialect 编译，
与 test_task_store_contract 同式。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.dml import Insert

from qqbot.services.agent_loop.meme_store import (
    MAX_SAVED_MEMES,
    MEME_SCOPE_GLOBAL,
    get_meme,
    insert_meme,
    load_saved_memes,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
BASE = datetime(2026, 7, 3, 12, 0, 0, tzinfo=SHANGHAI)
HASH_A = "ab" * 32
HASH_B = "cd" * 32


# ─── fakes ───


class _Result:
    def __init__(self, rows: list[Any] | None = None, rowcount: int = 1) -> None:
        self._rows = rows or []
        self.rowcount = rowcount

    def scalars(self) -> "_Result":
        return self

    def first(self) -> Any:
        return self._rows[0] if self._rows else None

    def all(self) -> list[Any]:
        return list(self._rows)


class _StubSession:
    """execute 捕获语句；select 回固定行、insert 回固定 rowcount。"""

    def __init__(self, owner: "_StubDB") -> None:
        self._owner = owner

    async def execute(self, stmt: Any) -> _Result:
        self._owner.statements.append(stmt)
        if isinstance(stmt, Insert):
            return _Result(rowcount=self._owner.insert_rowcount)
        return _Result(rows=self._owner.select_rows)

    async def commit(self) -> None:
        self._owner.commits += 1

    async def __aenter__(self) -> "_StubSession":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None


class _StubDB:
    def __init__(
        self,
        select_rows: list[Any] | None = None,
        insert_rowcount: int = 1,
    ) -> None:
        self.select_rows = list(select_rows or [])
        self.insert_rowcount = insert_rowcount
        self.statements: list[Any] = []
        self.commits = 0

    def factory(self) -> _StubSession:
        return _StubSession(self)


def _meme_row(
    *,
    file_hash: str = HASH_A,
    description: str = "黑猫瞪眼，嘲讽用",
    created_at: datetime | None = None,
) -> SimpleNamespace:
    """伪 AgentMeme ORM row（_row_to_meme_view 只读这些属性）。"""
    return SimpleNamespace(
        file_hash=file_hash,
        description=description,
        created_at=created_at or BASE,
    )


def _sql(stmt: Any) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))


class InsertMemeTests(unittest.IsolatedAsyncioTestCase):
    async def _insert(self, db: _StubDB) -> bool:
        return await insert_meme(
            db.factory,
            file_hash=HASH_A,
            description="黑猫瞪眼，嘲讽用",
            context_note="张三的名场面",
            mime="image/png",
            source_event_id="TC_EVENT",
            created_at=BASE,
        )

    async def test_insert_writes_all_columns_with_conflict_target(self) -> None:
        db = _StubDB()
        inserted = await self._insert(db)
        self.assertTrue(inserted)
        self.assertEqual(db.commits, 1)
        self.assertEqual(len(db.statements), 1)
        stmt = db.statements[0]
        self.assertIsInstance(stmt, Insert)
        params = stmt.compile(dialect=postgresql.dialect()).params
        # 全局共享：scope_key 固定落哨兵，不接受调用方传入
        self.assertEqual(params["scope_key"], MEME_SCOPE_GLOBAL)
        self.assertEqual(params["file_hash"], HASH_A)
        self.assertEqual(params["description"], "黑猫瞪眼，嘲讽用")
        self.assertEqual(params["context_note"], "张三的名场面")
        self.assertEqual(params["mime"], "image/png")
        self.assertEqual(params["source_event_id"], "TC_EVENT")
        self.assertEqual(params["created_at"], BASE)
        # 冲突语义：ON CONFLICT (主键) DO NOTHING —— 重复收藏不覆盖
        sql = _sql(stmt)
        self.assertIn("ON CONFLICT (scope_key, file_hash) DO NOTHING", sql)

    async def test_insert_conflict_returns_false(self) -> None:
        db = _StubDB(insert_rowcount=0)
        inserted = await self._insert(db)
        self.assertFalse(inserted)


class GetMemeTests(unittest.IsolatedAsyncioTestCase):
    async def test_hit_maps_row_to_view(self) -> None:
        db = _StubDB(select_rows=[_meme_row()])
        view = await get_meme(db.factory, HASH_A)
        self.assertIsNotNone(view)
        assert view is not None
        self.assertEqual(view.file_hash, HASH_A)
        self.assertEqual(view.description, "黑猫瞪眼，嘲讽用")
        self.assertEqual(view.saved_at, BASE)

    async def test_miss_returns_none(self) -> None:
        db = _StubDB(select_rows=[])
        view = await get_meme(db.factory, HASH_A)
        self.assertIsNone(view)

    async def test_statement_filters_global_sentinel(self) -> None:
        # 全局共享的实现锚点：查询按哨兵 scope_key 过滤（未迁移的旧分群行
        # 不可见），而不是不带 scope 条件裸查 hash。
        db = _StubDB(select_rows=[])
        await get_meme(db.factory, HASH_A)
        params = db.statements[0].compile(dialect=postgresql.dialect()).params
        self.assertIn(MEME_SCOPE_GLOBAL, params.values())


class LoadSavedMemesTests(unittest.IsolatedAsyncioTestCase):
    async def test_maps_rows_and_normalizes_timezone(self) -> None:
        utc_row = _meme_row(
            file_hash=HASH_B,
            created_at=datetime(2026, 7, 3, 4, 0, 0, tzinfo=timezone.utc),
        )
        db = _StubDB(select_rows=[_meme_row(), utc_row])
        views = await load_saved_memes(db.factory)
        self.assertEqual([v.file_hash for v in views], [HASH_A, HASH_B])
        # UTC 04:00 → 北京 12:00（asyncpg 读回 UTC，读侧统一 normalize）
        self.assertEqual(views[1].saved_at.tzinfo, SHANGHAI)
        self.assertEqual(views[1].saved_at.hour, 12)

    async def test_statement_orders_desc_and_limits(self) -> None:
        db = _StubDB(select_rows=[])
        await load_saved_memes(db.factory)
        sql = _sql(db.statements[0])
        self.assertIn("ORDER BY agent_memes.created_at DESC", sql)
        self.assertIn("LIMIT", sql)
        # 与 get_meme 同：全局哨兵过滤
        params = db.statements[0].compile(dialect=postgresql.dialect()).params
        self.assertIn(MEME_SCOPE_GLOBAL, params.values())

    async def test_default_limit_is_capped(self) -> None:
        # <saved-memes> 是每 tick 注入的 prompt 区，默认上限必须存在且有限。
        self.assertGreater(MAX_SAVED_MEMES, 0)
        self.assertLessEqual(MAX_SAVED_MEMES, 200)


if __name__ == "__main__":
    unittest.main()

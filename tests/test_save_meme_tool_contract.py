"""Contract tests for SaveMemeTool（表情包收录）。

设计结论（表情包工具黑盒设计.md §save_meme）：
- image_hash 必须是 timeline 里出现过、且 EventIngest 已落盘的图（磁盘存在性
  = "bot 真的见过"）；大小写归一小写；非法 → invalid_arguments
  (reason_code=bad_image_hash)，盘上没有 → image_not_found。
- 描述由 context 注入的 caption_image 回调生成（工具不自己 import LLM）：
  未注入 → internal_tool_error；调用异常 / 空输出 → caption_failed
  （retryable=true）且**不落表**——不留无描述的残记录。
- 该 hash 已收录（收藏夹 2026-07-06 起全局共享，落表用哨兵 scope_key
  'global'）→ already_saved + 现有描述，不重复 caption、不覆盖；INSERT
  冲突（并发竞争）同样折 already_saved。
- mime 从文件 magic bytes 嗅探，随 caption 调用与落表。
- allowed_scopes=("group","private")：system scope 硬调 →
  tool_unavailable_in_scope。

全程无 raise；不打真实 DB / LLM / 磁盘目录（tempdir + patch MEDIA_IMG_DIR）。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.dml import Insert

from qqbot.services.agent_loop.tools.save_meme import SaveMemeTool


def _params(stmt: Any) -> dict:
    """ON CONFLICT 是 PG 专属子句，必须用 postgresql dialect 编译。"""
    return stmt.compile(dialect=postgresql.dialect()).params

HASH_A = "ab" * 32
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake-png-body"
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"fake-jpeg-body"


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


class _FakeMemeDB:
    """select 依次弹出 select_results（每次 execute 一组行）；insert 固定
    rowcount。捕获全部语句供断言。"""

    def __init__(
        self,
        select_results: list[list[Any]] | None = None,
        insert_rowcount: int = 1,
    ) -> None:
        self.select_results = list(select_results or [])
        self.insert_rowcount = insert_rowcount
        self.statements: list[Any] = []
        self.commits = 0

    def factory(self) -> "_FakeSession":
        return _FakeSession(self)

    @property
    def insert_statements(self) -> list[Any]:
        return [s for s in self.statements if isinstance(s, Insert)]


class _FakeSession:
    def __init__(self, owner: _FakeMemeDB) -> None:
        self._owner = owner

    async def execute(self, stmt: Any) -> _Result:
        self._owner.statements.append(stmt)
        if isinstance(stmt, Insert):
            return _Result(rowcount=self._owner.insert_rowcount)
        rows = (
            self._owner.select_results.pop(0)
            if self._owner.select_results
            else []
        )
        return _Result(rows=rows)

    async def commit(self) -> None:
        self._owner.commits += 1

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None


def _meme_row(description: str = "已有的描述") -> SimpleNamespace:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    return SimpleNamespace(
        file_hash=HASH_A,
        description=description,
        created_at=datetime(2026, 7, 3, 12, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )


def _captioner(description: str = "黑猫瞪眼，配字就这，嘲讽用"):
    """假 caption 回调：记录 (bytes, mime, note) 调用，返回固定描述。"""
    calls: list[tuple[bytes, str, str | None]] = []

    async def caption(data: bytes, mime: str, note: str | None) -> str:
        calls.append((data, mime, note))
        return description

    return caption, calls


def _failing_captioner(exc: Exception):
    async def caption(data: bytes, mime: str, note: str | None) -> str:
        raise exc

    return caption


class SaveMemeToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.media_root = Path(self._tmp.name)
        patcher = patch(
            "qqbot.services.agent_loop.tools._meme_common.MEDIA_IMG_DIR",
            self.media_root,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_media(self, file_hash: str, data: bytes = PNG_BYTES) -> None:
        path = self.media_root / file_hash[:2] / file_hash
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def _context(
        self,
        db: _FakeMemeDB,
        captioner: Any,
        scope_key: str = "group:100",
    ) -> dict:
        return {
            "scope_key": scope_key,
            "session_factory": db.factory,
            "caption_image": captioner,
            "correlation_id": "CID",
            "tool_call_event_id": "TC_EVENT",
        }

    # ── happy path ──

    async def test_save_generates_description_and_inserts(self) -> None:
        self._write_media(HASH_A)
        db = _FakeMemeDB(select_results=[[]])  # 前查未命中
        caption, calls = _captioner()
        outcome = await SaveMemeTool().run(
            {"image_hash": HASH_A, "context_note": "张三的名场面"},
            **self._context(db, caption),
        )
        self.assertTrue(outcome.ok)
        self.assertTrue(outcome.result["saved"])
        self.assertEqual(outcome.result["file_hash"], HASH_A)
        self.assertEqual(outcome.result["description"], "黑猫瞪眼，配字就这，嘲讽用")
        # captioner 收到的是文件 bytes + 嗅探 mime + 语境 note
        self.assertEqual(calls, [(PNG_BYTES, "image/png", "张三的名场面")])
        # 落表 values 完整；scope_key 固定落全局哨兵（不是发起群）
        self.assertEqual(len(db.insert_statements), 1)
        params = _params(db.insert_statements[0])
        self.assertEqual(params["scope_key"], "global")
        self.assertEqual(params["file_hash"], HASH_A)
        self.assertEqual(params["description"], "黑猫瞪眼，配字就这，嘲讽用")
        self.assertEqual(params["context_note"], "张三的名场面")
        self.assertEqual(params["mime"], "image/png")
        self.assertEqual(params["source_event_id"], "TC_EVENT")

    async def test_uppercase_hash_normalized_to_lower(self) -> None:
        self._write_media(HASH_A)
        db = _FakeMemeDB(select_results=[[]])
        caption, _ = _captioner()
        outcome = await SaveMemeTool().run(
            {"image_hash": HASH_A.upper()}, **self._context(db, caption)
        )
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.result["file_hash"], HASH_A)

    async def test_jpeg_magic_sniffed_as_jpeg(self) -> None:
        self._write_media(HASH_A, JPEG_BYTES)
        db = _FakeMemeDB(select_results=[[]])
        caption, calls = _captioner()
        outcome = await SaveMemeTool().run(
            {"image_hash": HASH_A}, **self._context(db, caption)
        )
        self.assertTrue(outcome.ok)
        self.assertEqual(calls[0][1], "image/jpeg")
        params = _params(db.insert_statements[0])
        self.assertEqual(params["mime"], "image/jpeg")

    # ── 已收录 / 并发冲突 ──

    async def test_already_saved_skips_caption_and_insert(self) -> None:
        self._write_media(HASH_A)
        db = _FakeMemeDB(select_results=[[_meme_row("已有的描述")]])
        caption, calls = _captioner()
        outcome = await SaveMemeTool().run(
            {"image_hash": HASH_A}, **self._context(db, caption)
        )
        self.assertTrue(outcome.ok)
        self.assertTrue(outcome.result["already_saved"])
        self.assertEqual(outcome.result["description"], "已有的描述")
        self.assertEqual(calls, [])  # 不重复 caption
        self.assertEqual(db.insert_statements, [])  # 不覆盖

    async def test_insert_conflict_race_returns_already_saved(self) -> None:
        # 前查未命中 → caption → INSERT 冲突（并发另一次保存先到）→ 复查回执
        self._write_media(HASH_A)
        db = _FakeMemeDB(
            select_results=[[], [_meme_row("对方先存的描述")]],
            insert_rowcount=0,
        )
        caption, _ = _captioner()
        outcome = await SaveMemeTool().run(
            {"image_hash": HASH_A}, **self._context(db, caption)
        )
        self.assertTrue(outcome.ok)
        self.assertTrue(outcome.result["already_saved"])
        self.assertEqual(outcome.result["description"], "对方先存的描述")

    # ── 失败面 ──

    async def test_bad_hash_invalid_arguments(self) -> None:
        db = _FakeMemeDB()
        caption, _ = _captioner()
        for bad in ("xyz", "ab" * 31, "", None, 123):
            outcome = await SaveMemeTool().run(
                {"image_hash": bad}, **self._context(db, caption)
            )
            self.assertFalse(outcome.ok)
            self.assertEqual(outcome.error_kind, "invalid_arguments")
            self.assertEqual(outcome.extra.get("reason_code"), "bad_image_hash")

    async def test_file_missing_is_image_not_found(self) -> None:
        db = _FakeMemeDB()
        caption, calls = _captioner()
        outcome = await SaveMemeTool().run(
            {"image_hash": HASH_A}, **self._context(db, caption)
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "image_not_found")
        self.assertEqual(calls, [])

    async def test_caption_exception_is_caption_failed_no_insert(self) -> None:
        self._write_media(HASH_A)
        db = _FakeMemeDB(select_results=[[]])
        outcome = await SaveMemeTool().run(
            {"image_hash": HASH_A},
            **self._context(db, _failing_captioner(RuntimeError("llm down"))),
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "caption_failed")
        self.assertTrue(outcome.extra.get("retryable"))
        self.assertEqual(db.insert_statements, [])  # 不落无描述残记录

    async def test_empty_caption_is_caption_failed(self) -> None:
        self._write_media(HASH_A)
        db = _FakeMemeDB(select_results=[[]])
        caption, _ = _captioner(description="   ")
        outcome = await SaveMemeTool().run(
            {"image_hash": HASH_A}, **self._context(db, caption)
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "caption_failed")
        self.assertEqual(db.insert_statements, [])

    async def test_missing_captioner_is_internal_tool_error(self) -> None:
        self._write_media(HASH_A)
        db = _FakeMemeDB(select_results=[[]])
        ctx = self._context(db, None)
        outcome = await SaveMemeTool().run({"image_hash": HASH_A}, **ctx)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "internal_tool_error")

    async def test_context_note_must_be_string(self) -> None:
        self._write_media(HASH_A)
        db = _FakeMemeDB()
        caption, _ = _captioner()
        outcome = await SaveMemeTool().run(
            {"image_hash": HASH_A, "context_note": 42},
            **self._context(db, caption),
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "invalid_arguments")
        self.assertEqual(
            outcome.extra.get("reason_code"), "context_note_not_str"
        )

    async def test_system_scope_rejected(self) -> None:
        db = _FakeMemeDB()
        caption, _ = _captioner()
        outcome = await SaveMemeTool().run(
            {"image_hash": HASH_A},
            **self._context(db, caption, scope_key="system"),
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "tool_unavailable_in_scope")


class SniffMimeTests(unittest.TestCase):
    def test_known_magics(self) -> None:
        from qqbot.services.agent_loop.tools._meme_common import sniff_mime

        self.assertEqual(sniff_mime(PNG_BYTES), "image/png")
        self.assertEqual(sniff_mime(JPEG_BYTES), "image/jpeg")
        self.assertEqual(sniff_mime(b"GIF89a" + b"x"), "image/gif")
        self.assertEqual(
            sniff_mime(b"RIFF" + b"\x00" * 4 + b"WEBP" + b"x"), "image/webp"
        )
        # 未知内容兜底 png（只影响 data URL 标注，不致命）
        self.assertEqual(sniff_mime(b"whatever"), "image/png")


if __name__ == "__main__":
    unittest.main()

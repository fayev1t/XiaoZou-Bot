"""Contract tests for SendMemeTool（发送已收藏的表情包）。

设计结论（表情包工具黑盒设计.md §send_meme）：
- **收藏是发送的权限边界**：只发 agent_memes 全局收藏夹（2026-07-06 起全
  bot 共享）里命中的 hash；磁盘上有但没收录 → unknown_meme（bot 见过的
  任意图片不能被当表情包甩出）。
- 无 target 参数：目标从 scope_key 解析（隔离契约 §9，连跨会话的参数面都
  不给；收藏夹共享不放宽投递边界）；group → send_group_msg，
  private → send_private_msg。
- 发送段是 base64:// 内联单图（不赌 napcat 与本进程共享文件系统）。
- 结果契约对齐 send_message §8.3：上游 ok 但无 message_id → 视为
  upstream_action_failed(reason_code=missing_message_id)；成功 result 带
  message_id + self_id（投影 _build_author_index 折 from_self 依赖它）。
- 收藏在、文件没了 → media_file_missing（媒体目录被外部清理，违反契约）。

全程无 raise；stub Bot 进 bot_registry，assert 无一处 assertRaises。
"""

from __future__ import annotations

import base64
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from zoneinfo import ZoneInfo

from qqbot.services.agent_loop import bot_registry
from qqbot.services.agent_loop.tools.send_meme import SendMemeTool

HASH_A = "ab" * 32
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"meme-body"
BASE = datetime(2026, 7, 3, 12, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


class _FakeActionFailed(Exception):
    """模拟 nonebot ActionFailed：完整响应挂在 .info。"""

    def __init__(self, retcode: int, wording: str) -> None:
        super().__init__(f"ActionFailed: retcode={retcode}")
        self.info = {
            "status": "failed",
            "retcode": retcode,
            "message": "",
            "wording": wording,
            "stream": "normal-action",
        }


class _StubBot:
    def __init__(
        self,
        self_id: str = "10001",
        message_id: int | None = 12345,
        raise_exc: Exception | None = None,
    ) -> None:
        self.self_id = self_id
        self._message_id = message_id
        self._raise = raise_exc
        self.calls: list[tuple[str, dict]] = []

    def _result(self) -> dict:
        return {"message_id": self._message_id} if self._message_id is not None else {}

    async def send_group_msg(self, **kwargs: Any) -> dict:
        self.calls.append(("send_group_msg", kwargs))
        if self._raise is not None:
            raise self._raise
        return self._result()

    async def send_private_msg(self, **kwargs: Any) -> dict:
        self.calls.append(("send_private_msg", kwargs))
        if self._raise is not None:
            raise self._raise
        return self._result()


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> "_Result":
        return self

    def first(self) -> Any:
        return self._rows[0] if self._rows else None

    def all(self) -> list[Any]:
        return list(self._rows)


class _MemeReadDB:
    """get_meme 只有 select 面：固定返回 rows。"""

    def __init__(self, rows: list[Any] | None = None) -> None:
        self.rows = list(rows or [])

    def factory(self) -> "_MemeReadSession":
        return _MemeReadSession(self.rows)


class _MemeReadSession:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    async def execute(self, stmt: Any) -> _Result:
        return _Result(self._rows)

    async def commit(self) -> None:
        return None

    async def __aenter__(self) -> "_MemeReadSession":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None


def _meme_row() -> SimpleNamespace:
    return SimpleNamespace(
        file_hash=HASH_A, description="黑猫瞪眼", created_at=BASE
    )


class SendMemeToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        bot_registry.clear()
        self.addCleanup(bot_registry.clear)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.media_root = Path(self._tmp.name)
        patcher = patch(
            "qqbot.services.agent_loop.tools._meme_common.MEDIA_IMG_DIR",
            self.media_root,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_media(self, file_hash: str = HASH_A, data: bytes = PNG_BYTES) -> None:
        path = self.media_root / file_hash[:2] / file_hash
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def _context(
        self, db: _MemeReadDB, scope_key: str = "group:100"
    ) -> dict:
        return {
            "scope_key": scope_key,
            "session_factory": db.factory,
            "correlation_id": "CID",
            "tool_call_event_id": "TC_EVENT",
        }

    # ── happy path ──

    async def test_group_send_base64_image_segment(self) -> None:
        self._write_media()
        bot = _StubBot(message_id=999)
        bot_registry.register(bot)
        db = _MemeReadDB(rows=[_meme_row()])
        outcome = await SendMemeTool().run(
            {"image_hash": HASH_A}, **self._context(db)
        )
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.result["message_id"], 999)
        self.assertEqual(outcome.result["self_id"], "10001")
        self.assertEqual(outcome.result["file_hash"], HASH_A)
        self.assertTrue(outcome.result["sent"])
        # 调用面：send_group_msg + 单个 image 段 + base64:// 可还原原始 bytes
        self.assertEqual(len(bot.calls), 1)
        method, kwargs = bot.calls[0]
        self.assertEqual(method, "send_group_msg")
        self.assertEqual(kwargs["group_id"], 100)
        message = kwargs["message"]
        self.assertEqual(len(message), 1)
        self.assertEqual(message[0]["type"], "image")
        file_value = message[0]["data"]["file"]
        self.assertTrue(file_value.startswith("base64://"))
        self.assertEqual(
            base64.b64decode(file_value[len("base64://"):]), PNG_BYTES
        )

    async def test_private_send_routes_by_scope(self) -> None:
        self._write_media()
        bot = _StubBot()
        bot_registry.register(bot)
        db = _MemeReadDB(rows=[_meme_row()])
        outcome = await SendMemeTool().run(
            {"image_hash": HASH_A}, **self._context(db, scope_key="private:555")
        )
        self.assertTrue(outcome.ok)
        method, kwargs = bot.calls[0]
        self.assertEqual(method, "send_private_msg")
        self.assertEqual(kwargs["user_id"], 555)

    # ── 收藏边界 ──

    async def test_unsaved_hash_is_unknown_meme_and_no_send(self) -> None:
        # 磁盘上有这张图（bot 见过），但全局收藏夹里没收录过 → 拒发。
        self._write_media()
        bot = _StubBot()
        bot_registry.register(bot)
        db = _MemeReadDB(rows=[])
        outcome = await SendMemeTool().run(
            {"image_hash": HASH_A}, **self._context(db)
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "unknown_meme")
        self.assertEqual(bot.calls, [])

    async def test_saved_but_file_gone_is_media_file_missing(self) -> None:
        bot = _StubBot()
        bot_registry.register(bot)
        db = _MemeReadDB(rows=[_meme_row()])  # 收藏在，文件不写
        outcome = await SendMemeTool().run(
            {"image_hash": HASH_A}, **self._context(db)
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "media_file_missing")
        self.assertEqual(bot.calls, [])

    # ── 发送失败面 ──

    async def test_missing_message_id_is_upstream_failure(self) -> None:
        self._write_media()
        bot_registry.register(_StubBot(message_id=None))
        db = _MemeReadDB(rows=[_meme_row()])
        outcome = await SendMemeTool().run(
            {"image_hash": HASH_A}, **self._context(db)
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "upstream_action_failed")
        self.assertEqual(
            outcome.extra.get("reason_code"), "missing_message_id"
        )

    async def test_action_failed_folds_upstream_failure(self) -> None:
        self._write_media()
        bot_registry.register(
            _StubBot(raise_exc=_FakeActionFailed(100, "被禁言"))
        )
        db = _MemeReadDB(rows=[_meme_row()])
        outcome = await SendMemeTool().run(
            {"image_hash": HASH_A}, **self._context(db)
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "upstream_action_failed")
        self.assertEqual(outcome.extra.get("retcode"), 100)
        self.assertIn("被禁言", outcome.error_message or "")

    async def test_no_bot_available(self) -> None:
        self._write_media()
        db = _MemeReadDB(rows=[_meme_row()])
        outcome = await SendMemeTool().run(
            {"image_hash": HASH_A}, **self._context(db)
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "no_bot_available")

    # ── 参数 / scope ──

    async def test_bad_hash_invalid_arguments(self) -> None:
        db = _MemeReadDB()
        for bad in ("xyz", "", None):
            outcome = await SendMemeTool().run(
                {"image_hash": bad}, **self._context(db)
            )
            self.assertFalse(outcome.ok)
            self.assertEqual(outcome.error_kind, "invalid_arguments")
            self.assertEqual(outcome.extra.get("reason_code"), "bad_image_hash")

    async def test_system_scope_rejected(self) -> None:
        db = _MemeReadDB()
        outcome = await SendMemeTool().run(
            {"image_hash": HASH_A}, **self._context(db, scope_key="system")
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "tool_unavailable_in_scope")


if __name__ == "__main__":
    unittest.main()

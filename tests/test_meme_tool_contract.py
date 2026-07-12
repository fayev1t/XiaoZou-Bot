"""Contract tests for MemeTool（表情包一站式工具，action=save/send/delete/
recaption）。

2026-07-12 起 save_meme / send_meme（2026-07-03）与当晚拆分先行的
delete_meme / recaption_meme 合并为单工具 `meme`；本文件整合原四份工具契约
测试（test_save_meme / test_send_meme / test_delete_meme /
test_recaption_meme_tool_contract.py，均已删除）。

设计结论（表情包工具黑盒设计.md）：
- 工具面：action 必填且限 save/send/delete/recaption（非法 →
  invalid_arguments reason_code=bad_action）；image_hash 四动作共用（大小写
  归一小写，非法 → bad_image_hash）；context_note 仅 save/recaption 消费，
  给其它动作 → context_note_not_applicable，非字符串 → context_note_not_str；
  allowed_scopes=("group","private")，system scope 硬调 →
  tool_unavailable_in_scope。
- save：磁盘存在性 = "bot 真的见过"（盘上没有 → image_not_found）；描述由
  context 注入的 caption_image 生成（未注入 → internal_tool_error，异常/空
  输出 → caption_failed 且**不落表**）；已收录 → already_saved 不重复
  caption 不覆盖；INSERT 冲突（并发）同折 already_saved。
- save 批量（image_hash 传数组，黑盒设计 §3.1）：结构错整调拒绝（空 →
  empty_batch、超 MAX_SAVE_BATCH → too_many_images、任一元素非法 →
  bad_image_hash + batch_index）、同批重复静默去重保序；content 失败逐项
  承担（逐张走单张流程、逐项回执 results）；≥1 张成功 → success（batch:
  true + 三计数），无一成功 → batch_save_failed（retryable=任一项
  retryable）；单 string 输入结果形态不变（不包 batch 壳）。
- send：**收藏是发送的权限边界**（未收录 → unknown_meme 拒发）；无 target
  参数，目标从 scope_key 解析（group → send_group_msg，private →
  send_private_msg）；base64:// 内联单图；上游 ok 但无 message_id →
  upstream_action_failed(missing_message_id)；成功 result 带 message_id +
  self_id（投影 _build_author_index 折 from_self 依赖它）；收藏在、文件没了
  → media_file_missing。
- delete：未收录 → unknown_meme 不发 DELETE；命中 → 删元数据并回执被删条目
  描述（确认话术点名绑定对象）；并发删除（rowcount=0）结果状态一致照常
  success。
- recaption：未收录 → unknown_meme 不读盘不 caption；语境新 note 优先、未
  提供（或空串）沿用收录时留档的旧语境；caption 失败不落表旧描述保留；
  UPDATE rowcount=0（并发被删）→ unknown_meme。

全程无 raise；不打真实 DB / LLM / 磁盘目录 / napcat（fake session 捕获语句、
假 captioner、tempdir + patch MEDIA_IMG_DIR、stub Bot 进 bot_registry）。
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

from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.dml import Delete, Insert, Update

from qqbot.services.agent_loop import bot_registry
from qqbot.services.agent_loop.tools.meme import MAX_SAVE_BATCH, MemeTool

HASH_A = "ab" * 32
HASH_B = "cd" * 32
HASH_C = "ef" * 32
BASE = datetime(2026, 7, 3, 12, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake-png-body"
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"fake-jpeg-body"


def _params(stmt: Any) -> dict:
    """ON CONFLICT 是 PG 专属子句，必须用 postgresql dialect 编译。"""
    return stmt.compile(dialect=postgresql.dialect()).params


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
    """select 依次弹出 select_results（每次 execute 一组行）；DML
    （insert/update/delete）回固定 rowcount。捕获全部语句供断言。"""

    def __init__(
        self,
        select_results: list[list[Any]] | None = None,
        dml_rowcount: int = 1,
    ) -> None:
        self.select_results = list(select_results or [])
        self.dml_rowcount = dml_rowcount
        self.statements: list[Any] = []
        self.commits = 0

    def factory(self) -> "_FakeSession":
        return _FakeSession(self)

    def _dml(self, kind: type) -> list[Any]:
        return [s for s in self.statements if isinstance(s, kind)]

    @property
    def insert_statements(self) -> list[Any]:
        return self._dml(Insert)

    @property
    def update_statements(self) -> list[Any]:
        return self._dml(Update)

    @property
    def delete_statements(self) -> list[Any]:
        return self._dml(Delete)


class _FakeSession:
    def __init__(self, owner: _FakeMemeDB) -> None:
        self._owner = owner

    async def execute(self, stmt: Any) -> _Result:
        self._owner.statements.append(stmt)
        if isinstance(stmt, (Insert, Update, Delete)):
            return _Result(rowcount=self._owner.dml_rowcount)
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


def _meme_row(
    description: str = "黑猫瞪眼，嘲讽用",
    context_note: str | None = "张三的名场面",
    file_hash: str = HASH_A,
) -> SimpleNamespace:
    """伪 AgentMeme ORM row（_row_to_meme_view 只读这些属性）。"""
    return SimpleNamespace(
        file_hash=file_hash,
        description=description,
        created_at=BASE,
        context_note=context_note,
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


class _MemeToolTestBase(unittest.IsolatedAsyncioTestCase):
    """公共脚手架：临时 media 目录 + bot_registry 清理 + context 构造。"""

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
        self,
        db: _FakeMemeDB,
        captioner: Any = None,
        scope_key: str = "group:100",
    ) -> dict:
        return {
            "scope_key": scope_key,
            "session_factory": db.factory,
            "caption_image": captioner,
            "correlation_id": "CID",
            "tool_call_event_id": "TC_EVENT",
        }


class ActionDispatchTests(_MemeToolTestBase):
    """工具面：action 校验 / context_note 适用性 / scope / 公共失败。"""

    async def test_missing_or_bad_action_is_invalid_arguments(self) -> None:
        db = _FakeMemeDB()
        for bad in (None, "", "post", "SAVE", 42):
            outcome = await MemeTool().run(
                {"action": bad, "image_hash": HASH_A}, **self._context(db)
            )
            self.assertFalse(outcome.ok)
            self.assertEqual(outcome.error_kind, "invalid_arguments")
            self.assertEqual(outcome.extra.get("reason_code"), "bad_action")
        self.assertEqual(db.statements, [])

    async def test_bad_hash_invalid_arguments_for_every_action(self) -> None:
        db = _FakeMemeDB()
        for action in ("save", "send", "delete", "recaption"):
            for bad in ("xyz", "ab" * 31, "", None, 123):
                outcome = await MemeTool().run(
                    {"action": action, "image_hash": bad}, **self._context(db)
                )
                self.assertFalse(outcome.ok)
                self.assertEqual(outcome.error_kind, "invalid_arguments")
                self.assertEqual(
                    outcome.extra.get("reason_code"), "bad_image_hash"
                )
        self.assertEqual(db.statements, [])

    async def test_context_note_rejected_for_send_and_delete(self) -> None:
        db = _FakeMemeDB()
        for action in ("send", "delete"):
            outcome = await MemeTool().run(
                {
                    "action": action,
                    "image_hash": HASH_A,
                    "context_note": "语境",
                },
                **self._context(db),
            )
            self.assertFalse(outcome.ok)
            self.assertEqual(outcome.error_kind, "invalid_arguments")
            self.assertEqual(
                outcome.extra.get("reason_code"), "context_note_not_applicable"
            )

    async def test_context_note_must_be_string(self) -> None:
        db = _FakeMemeDB()
        for action in ("save", "recaption"):
            outcome = await MemeTool().run(
                {"action": action, "image_hash": HASH_A, "context_note": 42},
                **self._context(db),
            )
            self.assertFalse(outcome.ok)
            self.assertEqual(outcome.error_kind, "invalid_arguments")
            self.assertEqual(
                outcome.extra.get("reason_code"), "context_note_not_str"
            )

    async def test_system_scope_rejected(self) -> None:
        db = _FakeMemeDB()
        for action in ("save", "send", "delete", "recaption"):
            outcome = await MemeTool().run(
                {"action": action, "image_hash": HASH_A},
                **self._context(db, scope_key="system"),
            )
            self.assertFalse(outcome.ok)
            self.assertEqual(outcome.error_kind, "tool_unavailable_in_scope")

    async def test_missing_session_factory_is_internal_tool_error(self) -> None:
        db = _FakeMemeDB()
        for action in ("save", "send", "delete", "recaption"):
            ctx = self._context(db)
            ctx["session_factory"] = None
            outcome = await MemeTool().run(
                {"action": action, "image_hash": HASH_A}, **ctx
            )
            self.assertFalse(outcome.ok)
            self.assertEqual(outcome.error_kind, "internal_tool_error")


class SaveActionTests(_MemeToolTestBase):
    # ── happy path ──

    async def test_save_generates_description_and_inserts(self) -> None:
        self._write_media()
        db = _FakeMemeDB(select_results=[[]])  # 前查未命中
        caption, calls = _captioner()
        outcome = await MemeTool().run(
            {
                "action": "save",
                "image_hash": HASH_A,
                "context_note": "张三的名场面",
            },
            **self._context(db, caption),
        )
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.result["action"], "save")
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
        self._write_media()
        db = _FakeMemeDB(select_results=[[]])
        caption, _ = _captioner()
        outcome = await MemeTool().run(
            {"action": "save", "image_hash": HASH_A.upper()},
            **self._context(db, caption),
        )
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.result["file_hash"], HASH_A)

    async def test_jpeg_magic_sniffed_as_jpeg(self) -> None:
        self._write_media(data=JPEG_BYTES)
        db = _FakeMemeDB(select_results=[[]])
        caption, calls = _captioner()
        outcome = await MemeTool().run(
            {"action": "save", "image_hash": HASH_A},
            **self._context(db, caption),
        )
        self.assertTrue(outcome.ok)
        self.assertEqual(calls[0][1], "image/jpeg")
        params = _params(db.insert_statements[0])
        self.assertEqual(params["mime"], "image/jpeg")

    # ── 已收录 / 并发冲突 ──

    async def test_already_saved_skips_caption_and_insert(self) -> None:
        self._write_media()
        db = _FakeMemeDB(select_results=[[_meme_row("已有的描述")]])
        caption, calls = _captioner()
        outcome = await MemeTool().run(
            {"action": "save", "image_hash": HASH_A},
            **self._context(db, caption),
        )
        self.assertTrue(outcome.ok)
        self.assertTrue(outcome.result["already_saved"])
        self.assertEqual(outcome.result["description"], "已有的描述")
        self.assertEqual(calls, [])  # 不重复 caption
        self.assertEqual(db.insert_statements, [])  # 不覆盖

    async def test_insert_conflict_race_returns_already_saved(self) -> None:
        # 前查未命中 → caption → INSERT 冲突（并发另一次保存先到）→ 复查回执
        self._write_media()
        db = _FakeMemeDB(
            select_results=[[], [_meme_row("对方先存的描述")]],
            dml_rowcount=0,
        )
        caption, _ = _captioner()
        outcome = await MemeTool().run(
            {"action": "save", "image_hash": HASH_A},
            **self._context(db, caption),
        )
        self.assertTrue(outcome.ok)
        self.assertTrue(outcome.result["already_saved"])
        self.assertEqual(outcome.result["description"], "对方先存的描述")

    # ── 失败面 ──

    async def test_file_missing_is_image_not_found(self) -> None:
        db = _FakeMemeDB()
        caption, calls = _captioner()
        outcome = await MemeTool().run(
            {"action": "save", "image_hash": HASH_A},
            **self._context(db, caption),
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "image_not_found")
        self.assertEqual(calls, [])

    async def test_caption_exception_is_caption_failed_no_insert(self) -> None:
        self._write_media()
        db = _FakeMemeDB(select_results=[[]])
        outcome = await MemeTool().run(
            {"action": "save", "image_hash": HASH_A},
            **self._context(db, _failing_captioner(RuntimeError("llm down"))),
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "caption_failed")
        self.assertTrue(outcome.extra.get("retryable"))
        self.assertEqual(db.insert_statements, [])  # 不落无描述残记录

    async def test_empty_caption_is_caption_failed(self) -> None:
        self._write_media()
        db = _FakeMemeDB(select_results=[[]])
        caption, _ = _captioner(description="   ")
        outcome = await MemeTool().run(
            {"action": "save", "image_hash": HASH_A},
            **self._context(db, caption),
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "caption_failed")
        self.assertEqual(db.insert_statements, [])

    async def test_missing_captioner_is_internal_tool_error(self) -> None:
        self._write_media()
        db = _FakeMemeDB(select_results=[[]])
        outcome = await MemeTool().run(
            {"action": "save", "image_hash": HASH_A},
            **self._context(db, None),
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "internal_tool_error")


class BatchSaveActionTests(_MemeToolTestBase):
    """save 批量形态（黑盒设计 §3.1）：结构整拒 / 去重保序 / 逐项回执 /
    全败折 batch_save_failed / 单 string 形态不变。"""

    # ── happy path / 部分成功 ──

    async def test_batch_all_new_saves_each_and_counts(self) -> None:
        self._write_media(HASH_A)
        self._write_media(HASH_B)
        db = _FakeMemeDB(select_results=[[], []])  # 两张前查都未命中
        caption, calls = _captioner()
        outcome = await MemeTool().run(
            {"action": "save", "image_hash": [HASH_A, HASH_B]},
            **self._context(db, caption),
        )
        self.assertTrue(outcome.ok)
        self.assertTrue(outcome.result["batch"])
        self.assertEqual(outcome.result["saved_count"], 2)
        self.assertEqual(outcome.result["already_saved_count"], 0)
        self.assertEqual(outcome.result["failed_count"], 0)
        self.assertEqual(
            [r["file_hash"] for r in outcome.result["results"]],
            [HASH_A, HASH_B],  # 逐项回执保输入顺序
        )
        self.assertTrue(all(r["saved"] for r in outcome.result["results"]))
        self.assertEqual(len(calls), 2)  # 每张各 caption 一次
        self.assertEqual(len(db.insert_statements), 2)

    async def test_batch_partial_failure_reports_per_item(self) -> None:
        # A 新收录成功；B 已收录折 already_saved；C 盘上没有 → 逐项失败
        self._write_media(HASH_A)
        self._write_media(HASH_B)
        db = _FakeMemeDB(
            select_results=[[], [_meme_row(file_hash=HASH_B, description="旧的")]]
        )
        caption, calls = _captioner()
        outcome = await MemeTool().run(
            {"action": "save", "image_hash": [HASH_A, HASH_B, HASH_C]},
            **self._context(db, caption),
        )
        self.assertTrue(outcome.ok)  # 有成功项 → 整体 success
        self.assertEqual(outcome.result["saved_count"], 1)
        self.assertEqual(outcome.result["already_saved_count"], 1)
        self.assertEqual(outcome.result["failed_count"], 1)
        results = outcome.result["results"]
        self.assertTrue(results[0]["saved"])
        self.assertTrue(results[1]["already_saved"])
        self.assertEqual(results[2]["file_hash"], HASH_C)
        self.assertEqual(results[2]["error_kind"], "image_not_found")
        self.assertEqual(len(calls), 1)  # already_saved 与失败项都不 caption

    async def test_batch_duplicates_deduped_in_order(self) -> None:
        self._write_media(HASH_A)
        db = _FakeMemeDB(select_results=[[]])
        caption, calls = _captioner()
        outcome = await MemeTool().run(
            {"action": "save", "image_hash": [HASH_A, HASH_A.upper(), HASH_A]},
            **self._context(db, caption),
        )
        self.assertTrue(outcome.ok)
        self.assertEqual(len(outcome.result["results"]), 1)  # 去重后只处理一次
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(db.insert_statements), 1)

    async def test_batch_context_note_applies_to_every_item(self) -> None:
        self._write_media(HASH_A)
        self._write_media(HASH_B)
        db = _FakeMemeDB(select_results=[[], []])
        caption, calls = _captioner()
        outcome = await MemeTool().run(
            {
                "action": "save",
                "image_hash": [HASH_A, HASH_B],
                "context_note": "运动会名场面合集",
            },
            **self._context(db, caption),
        )
        self.assertTrue(outcome.ok)
        self.assertEqual([c[2] for c in calls], ["运动会名场面合集"] * 2)

    # ── 全败 → batch_save_failed ──

    async def test_batch_all_failed_is_batch_save_failed(self) -> None:
        # 两张都不在盘上（image_not_found，不可重试）→ 整体失败
        db = _FakeMemeDB()
        caption, _ = _captioner()
        outcome = await MemeTool().run(
            {"action": "save", "image_hash": [HASH_A, HASH_B]},
            **self._context(db, caption),
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "batch_save_failed")
        self.assertFalse(outcome.extra.get("retryable"))
        items = outcome.extra["results"]
        self.assertEqual(
            [i["error_kind"] for i in items],
            ["image_not_found", "image_not_found"],
        )

    async def test_batch_all_failed_retryable_if_any_item_retryable(self) -> None:
        # caption 失败是 retryable → 整体 batch_save_failed 也标 retryable
        self._write_media(HASH_A)
        db = _FakeMemeDB(select_results=[[]])
        outcome = await MemeTool().run(
            {"action": "save", "image_hash": [HASH_A]},
            **self._context(db, _failing_captioner(RuntimeError("llm down"))),
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "batch_save_failed")
        self.assertTrue(outcome.extra.get("retryable"))
        self.assertTrue(outcome.extra["results"][0].get("retryable"))

    # ── 结构类错误整调拒绝 ──

    async def test_empty_array_rejected(self) -> None:
        db = _FakeMemeDB()
        outcome = await MemeTool().run(
            {"action": "save", "image_hash": []}, **self._context(db)
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "invalid_arguments")
        self.assertEqual(outcome.extra.get("reason_code"), "empty_batch")

    async def test_over_cap_rejected(self) -> None:
        hashes = [f"{i:02x}" * 32 for i in range(MAX_SAVE_BATCH + 1)]
        db = _FakeMemeDB()
        outcome = await MemeTool().run(
            {"action": "save", "image_hash": hashes}, **self._context(db)
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "invalid_arguments")
        self.assertEqual(outcome.extra.get("reason_code"), "too_many_images")

    async def test_bad_hash_in_array_rejects_whole_call(self) -> None:
        self._write_media(HASH_A)
        db = _FakeMemeDB()
        caption, calls = _captioner()
        outcome = await MemeTool().run(
            {"action": "save", "image_hash": [HASH_A, "not-a-hash"]},
            **self._context(db, caption),
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "invalid_arguments")
        self.assertEqual(outcome.extra.get("reason_code"), "bad_image_hash")
        self.assertEqual(outcome.extra.get("batch_index"), 1)
        self.assertEqual(calls, [])  # 整调拒绝：合法的那张也不处理
        self.assertEqual(db.statements, [])

    async def test_array_rejected_for_other_actions(self) -> None:
        db = _FakeMemeDB()
        for action in ("send", "delete", "recaption"):
            outcome = await MemeTool().run(
                {"action": action, "image_hash": [HASH_A]},
                **self._context(db),
            )
            self.assertFalse(outcome.ok)
            self.assertEqual(outcome.error_kind, "invalid_arguments")
            self.assertEqual(
                outcome.extra.get("reason_code"), "batch_not_supported"
            )

    # ── 单 string 形态不变 ──

    async def test_single_string_result_has_no_batch_wrapper(self) -> None:
        self._write_media(HASH_A)
        db = _FakeMemeDB(select_results=[[]])
        caption, _ = _captioner()
        outcome = await MemeTool().run(
            {"action": "save", "image_hash": HASH_A},
            **self._context(db, caption),
        )
        self.assertTrue(outcome.ok)
        self.assertNotIn("batch", outcome.result)
        self.assertNotIn("results", outcome.result)
        self.assertTrue(outcome.result["saved"])


class SendActionTests(_MemeToolTestBase):
    # ── happy path ──

    async def test_group_send_base64_image_segment(self) -> None:
        self._write_media()
        bot = _StubBot(message_id=999)
        bot_registry.register(bot)
        db = _FakeMemeDB(select_results=[[_meme_row()]])
        outcome = await MemeTool().run(
            {"action": "send", "image_hash": HASH_A}, **self._context(db)
        )
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.result["action"], "send")
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
        db = _FakeMemeDB(select_results=[[_meme_row()]])
        outcome = await MemeTool().run(
            {"action": "send", "image_hash": HASH_A},
            **self._context(db, scope_key="private:555"),
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
        db = _FakeMemeDB(select_results=[[]])
        outcome = await MemeTool().run(
            {"action": "send", "image_hash": HASH_A}, **self._context(db)
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "unknown_meme")
        self.assertEqual(bot.calls, [])

    async def test_saved_but_file_gone_is_media_file_missing(self) -> None:
        bot = _StubBot()
        bot_registry.register(bot)
        db = _FakeMemeDB(select_results=[[_meme_row()]])  # 收藏在，文件不写
        outcome = await MemeTool().run(
            {"action": "send", "image_hash": HASH_A}, **self._context(db)
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "media_file_missing")
        self.assertEqual(bot.calls, [])

    # ── 发送失败面 ──

    async def test_missing_message_id_is_upstream_failure(self) -> None:
        self._write_media()
        bot_registry.register(_StubBot(message_id=None))
        db = _FakeMemeDB(select_results=[[_meme_row()]])
        outcome = await MemeTool().run(
            {"action": "send", "image_hash": HASH_A}, **self._context(db)
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
        db = _FakeMemeDB(select_results=[[_meme_row()]])
        outcome = await MemeTool().run(
            {"action": "send", "image_hash": HASH_A}, **self._context(db)
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "upstream_action_failed")
        self.assertEqual(outcome.extra.get("retcode"), 100)
        self.assertIn("被禁言", outcome.error_message or "")

    async def test_no_bot_available(self) -> None:
        self._write_media()
        db = _FakeMemeDB(select_results=[[_meme_row()]])
        outcome = await MemeTool().run(
            {"action": "send", "image_hash": HASH_A}, **self._context(db)
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "no_bot_available")


class DeleteActionTests(_MemeToolTestBase):
    async def test_delete_saved_meme_echoes_description(self) -> None:
        db = _FakeMemeDB(select_results=[[_meme_row()]])
        outcome = await MemeTool().run(
            {"action": "delete", "image_hash": HASH_A}, **self._context(db)
        )
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.result["action"], "delete")
        self.assertTrue(outcome.result["deleted"])
        self.assertEqual(outcome.result["file_hash"], HASH_A)
        # 被删条目描述回显：确认话术点名绑定对象
        self.assertEqual(outcome.result["description"], "黑猫瞪眼，嘲讽用")
        self.assertEqual(len(db.delete_statements), 1)

    async def test_delete_race_rowcount_zero_still_success(self) -> None:
        # 前查命中、DELETE 时别的删除先到：结果状态一致，照常回执 deleted
        db = _FakeMemeDB(select_results=[[_meme_row()]], dml_rowcount=0)
        outcome = await MemeTool().run(
            {"action": "delete", "image_hash": HASH_A}, **self._context(db)
        )
        self.assertTrue(outcome.ok)
        self.assertTrue(outcome.result["deleted"])

    async def test_unsaved_hash_is_unknown_meme_without_delete(self) -> None:
        db = _FakeMemeDB(select_results=[[]])
        outcome = await MemeTool().run(
            {"action": "delete", "image_hash": HASH_A}, **self._context(db)
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "unknown_meme")
        self.assertEqual(db.delete_statements, [])  # 未收录不发 DELETE


class RecaptionActionTests(_MemeToolTestBase):
    # ── happy path ──

    async def test_recaption_with_new_note_updates_description(self) -> None:
        self._write_media()
        db = _FakeMemeDB(select_results=[[_meme_row()]])
        caption, calls = _captioner("白猫叹气，配字摆烂，自嘲用")
        outcome = await MemeTool().run(
            {
                "action": "recaption",
                "image_hash": HASH_A,
                "context_note": "李四的名场面",
            },
            **self._context(db, caption),
        )
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.result["action"], "recaption")
        self.assertTrue(outcome.result["recaptioned"])
        self.assertEqual(outcome.result["file_hash"], HASH_A)
        self.assertEqual(outcome.result["description"], "白猫叹气，配字摆烂，自嘲用")
        # 旧描述回显：确认话术能说清改了什么
        self.assertEqual(outcome.result["previous_description"], "黑猫瞪眼，嘲讽用")
        # captioner 收到文件 bytes + 嗅探 mime + **新**语境
        self.assertEqual(calls, [(PNG_BYTES, "image/png", "李四的名场面")])
        # 落表：description + context_note（新语境同步留档）
        self.assertEqual(len(db.update_statements), 1)
        params = _params(db.update_statements[0])
        self.assertEqual(params["description"], "白猫叹气，配字摆烂，自嘲用")
        self.assertEqual(params["context_note"], "李四的名场面")

    async def test_omitted_note_reuses_saved_note(self) -> None:
        # context_note 未提供 → 沿用收录时留档的旧语境（留档备重生成的兑现点）
        self._write_media()
        db = _FakeMemeDB(select_results=[[_meme_row(context_note="张三的名场面")]])
        caption, calls = _captioner()
        outcome = await MemeTool().run(
            {"action": "recaption", "image_hash": HASH_A},
            **self._context(db, caption),
        )
        self.assertTrue(outcome.ok)
        self.assertEqual(calls[0][2], "张三的名场面")
        params = _params(db.update_statements[0])
        self.assertEqual(params["context_note"], "张三的名场面")

    async def test_blank_note_treated_as_omitted(self) -> None:
        self._write_media()
        db = _FakeMemeDB(select_results=[[_meme_row(context_note="张三的名场面")]])
        caption, calls = _captioner()
        outcome = await MemeTool().run(
            {"action": "recaption", "image_hash": HASH_A, "context_note": "   "},
            **self._context(db, caption),
        )
        self.assertTrue(outcome.ok)
        self.assertEqual(calls[0][2], "张三的名场面")

    # ── 失败面 ──

    async def test_unsaved_hash_is_unknown_meme_without_caption(self) -> None:
        self._write_media()
        db = _FakeMemeDB(select_results=[[]])
        caption, calls = _captioner()
        outcome = await MemeTool().run(
            {"action": "recaption", "image_hash": HASH_A},
            **self._context(db, caption),
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "unknown_meme")
        self.assertEqual(calls, [])  # 未收录不读盘不 caption
        self.assertEqual(db.update_statements, [])

    async def test_file_missing_is_media_file_missing(self) -> None:
        # 收藏在、文件没了：违反黑盒设计 §7 钉住约束的防御位
        db = _FakeMemeDB(select_results=[[_meme_row()]])
        caption, calls = _captioner()
        outcome = await MemeTool().run(
            {"action": "recaption", "image_hash": HASH_A},
            **self._context(db, caption),
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "media_file_missing")
        self.assertEqual(calls, [])

    async def test_caption_exception_keeps_old_description(self) -> None:
        self._write_media()
        db = _FakeMemeDB(select_results=[[_meme_row()]])
        outcome = await MemeTool().run(
            {"action": "recaption", "image_hash": HASH_A},
            **self._context(db, _failing_captioner(RuntimeError("llm down"))),
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "caption_failed")
        self.assertTrue(outcome.extra.get("retryable"))
        self.assertEqual(db.update_statements, [])  # 不落表，旧描述保留

    async def test_empty_caption_is_caption_failed(self) -> None:
        self._write_media()
        db = _FakeMemeDB(select_results=[[_meme_row()]])
        caption, _ = _captioner(description="   ")
        outcome = await MemeTool().run(
            {"action": "recaption", "image_hash": HASH_A},
            **self._context(db, caption),
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "caption_failed")
        self.assertEqual(db.update_statements, [])

    async def test_update_race_deleted_is_unknown_meme(self) -> None:
        # 前查命中、UPDATE 时收藏已被并发删除（rowcount=0）→ unknown_meme
        self._write_media()
        db = _FakeMemeDB(select_results=[[_meme_row()]], dml_rowcount=0)
        caption, _ = _captioner()
        outcome = await MemeTool().run(
            {"action": "recaption", "image_hash": HASH_A},
            **self._context(db, caption),
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "unknown_meme")

    async def test_missing_captioner_is_internal_tool_error(self) -> None:
        self._write_media()
        db = _FakeMemeDB(select_results=[[_meme_row()]])
        outcome = await MemeTool().run(
            {"action": "recaption", "image_hash": HASH_A},
            **self._context(db, None),
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "internal_tool_error")


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

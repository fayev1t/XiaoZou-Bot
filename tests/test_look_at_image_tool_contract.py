"""Contract tests for LookAtImageTool（带着具体问题重看一张图，2026-07-28）。

这个工具是 Planner/Replyer 去多模态化的能力兜底：timeline 里的图片只剩一段
ingest 期写好的**无语境客观转录**（`<image hash="..." desc="..."/>`），转录
覆盖不到的追问靠它现场重看原图。没有它，那次改动就是纯降级。

离线跑：`answer_about_image` 是被 patch 掉的（真实现要 langchain + 配置好的
vision 端点），落盘目录用 tempfile 顶替 MEDIA_IMG_DIR。
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from qqbot.core.permissions import PermissionTier
from qqbot.services.agent_loop.image_description import ImageLookError
from qqbot.services.agent_loop.tools.look_at_image import (
    MAX_QUESTION_CHARS,
    LookAtImageTool,
)

HASH = "a" * 64
PNG = b"\x89PNG\r\n\x1a\nfake-pixels"


class _PatchedMediaDir:
    """把 _meme_common 的落盘定位重定向到临时目录，并按 hash 写好文件。"""

    def __init__(self, tmp: Path, *, write: bool = True) -> None:
        self._tmp = tmp
        self._write = write
        self._patch: Any = None

    def __enter__(self) -> "_PatchedMediaDir":
        if self._write:
            path = self._tmp / HASH[:2] / HASH
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(PNG)
        self._patch = patch(
            "qqbot.services.agent_loop.tools._meme_common.MEDIA_IMG_DIR",
            self._tmp,
        )
        self._patch.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._patch.stop()


def _run(tool: LookAtImageTool, arguments: dict, **context: Any):
    return asyncio.run(tool.run(arguments, **context))


class ArgumentContractTests(unittest.TestCase):
    def test_question_is_required(self) -> None:
        """不带问题的调用 = 把 ingest 那次转录再跑一遍，而它已经在 timeline
        里了。必填挡住"再看一眼"式的偷懒调用。"""
        out = _run(LookAtImageTool(), {"image_hash": HASH})
        self.assertFalse(out.ok)
        self.assertEqual(out.error_kind, "invalid_arguments")
        self.assertEqual(out.extra.get("reason_code"), "bad_question")

    def test_blank_question_rejected(self) -> None:
        out = _run(LookAtImageTool(), {"image_hash": HASH, "question": "   "})
        self.assertFalse(out.ok)
        self.assertEqual(out.extra.get("reason_code"), "bad_question")

    def test_overlong_question_rejected(self) -> None:
        """问题超长说明模型在把 timeline 抄进来，而 VLM 并不需要那些。"""
        out = _run(
            LookAtImageTool(),
            {"image_hash": HASH, "question": "问" * (MAX_QUESTION_CHARS + 1)},
        )
        self.assertFalse(out.ok)
        self.assertEqual(out.extra.get("reason_code"), "question_too_long")

    def test_bad_hash_rejected(self) -> None:
        out = _run(
            LookAtImageTool(), {"image_hash": "not-a-hash", "question": "啥"}
        )
        self.assertFalse(out.ok)
        self.assertEqual(out.error_kind, "invalid_arguments")
        self.assertEqual(out.extra.get("reason_code"), "bad_image_hash")

    def test_schema_takes_exactly_two_arguments(self) -> None:
        """参数面刻意最小：不拆 context/task 两个槽，模型会把语境写进问题里
        （同 reply 工具 2026-07-25 收敛参数的取舍）。"""
        schema = LookAtImageTool.arguments_schema
        self.assertEqual(
            set(schema["properties"]), {"image_hash", "question"}
        )
        self.assertEqual(set(schema["required"]), {"image_hash", "question"})

    def test_tool_is_guest_and_scope_unrestricted(self) -> None:
        self.assertEqual(
            LookAtImageTool.required_permission, PermissionTier.GUEST
        )
        self.assertIsNone(LookAtImageTool.allowed_scopes)


class LookupTests(unittest.TestCase):
    def test_missing_file_folds_to_image_not_found(self) -> None:
        """hash 合法但盘上没有：抄错 hash / 图当初没下载成功 / 已被媒体 GC 清理。
        不是 retryable —— 重试也不会让文件长回来。"""
        with tempfile.TemporaryDirectory() as tmp:
            with _PatchedMediaDir(Path(tmp), write=False):
                out = _run(
                    LookAtImageTool(),
                    {"image_hash": HASH, "question": "这是啥"},
                )
        self.assertFalse(out.ok)
        self.assertEqual(out.error_kind, "image_not_found")
        self.assertFalse(out.extra.get("retryable"))

    def test_success_returns_answer_and_echoes_question(self) -> None:
        seen: dict[str, Any] = {}

        async def fake_answer(
            data: bytes, mime: str, file_hash: str, question: str
        ) -> str:
            seen.update(
                data=data, mime=mime, file_hash=file_hash, question=question
            )
            return "右下角写着 build 4213"

        with tempfile.TemporaryDirectory() as tmp:
            with _PatchedMediaDir(Path(tmp)):
                with patch(
                    "qqbot.services.agent_loop.tools.look_at_image."
                    "answer_about_image",
                    fake_answer,
                ):
                    out = _run(
                        LookAtImageTool(),
                        {
                            "image_hash": HASH,
                            "question": "  右下角那串数字是多少  ",
                        },
                    )

        self.assertTrue(out.ok)
        self.assertEqual(out.result["answer"], "右下角写着 build 4213")
        self.assertEqual(out.result["image_hash"], HASH)
        # question 去掉首尾空白后原样回执（也正是喂给 VLM 的那份）
        self.assertEqual(out.result["question"], "右下角那串数字是多少")
        self.assertEqual(seen["question"], "右下角那串数字是多少")
        self.assertEqual(seen["data"], PNG)
        self.assertEqual(seen["file_hash"], HASH)

    def test_vision_failure_is_retryable_upstream(self) -> None:
        """VLM 未配置/调用失败：对模型是"对面暂时不给看"，不是它参数错了。"""

        async def boom(
            data: bytes, mime: str, file_hash: str, question: str
        ) -> str:
            raise ImageLookError("vision LLM unavailable")

        with tempfile.TemporaryDirectory() as tmp:
            with _PatchedMediaDir(Path(tmp)):
                with patch(
                    "qqbot.services.agent_loop.tools.look_at_image."
                    "answer_about_image",
                    boom,
                ):
                    out = _run(
                        LookAtImageTool(),
                        {"image_hash": HASH, "question": "这是啥"},
                    )

        self.assertFalse(out.ok)
        self.assertEqual(out.error_kind, "upstream_action_failed")
        self.assertTrue(out.extra.get("retryable"))

    def test_unexpected_exception_folds_to_internal_error(self) -> None:
        """BaseTool.run 的兜底：预料外异常不越出工具边界（契约 §7.2）。"""

        async def kaboom(
            data: bytes, mime: str, file_hash: str, question: str
        ) -> str:
            raise RuntimeError("something nobody predicted")

        with tempfile.TemporaryDirectory() as tmp:
            with _PatchedMediaDir(Path(tmp)):
                with patch(
                    "qqbot.services.agent_loop.tools.look_at_image."
                    "answer_about_image",
                    kaboom,
                ):
                    out = _run(
                        LookAtImageTool(),
                        {"image_hash": HASH, "question": "这是啥"},
                    )

        self.assertFalse(out.ok)
        self.assertEqual(out.error_kind, "internal_tool_error")


class RegistrationTests(unittest.TestCase):
    def test_registered_in_default_registry(self) -> None:
        """新工具文件在 registry.register(...) 之前什么也不是（CLAUDE.md 硬规矩）。"""
        from qqbot.services.agent_loop.tools import build_default_registry

        self.assertIn("look_at_image", build_default_registry().names())

    def test_usage_doc_is_loaded(self) -> None:
        """sibling .md 缺失只 warning 不报错，会静默抽掉这个工具的全部说明——
        钉一下，重命名/移动文件时立刻发现。"""
        self.assertIn("look_at_image", LookAtImageTool.usage_prompt)
        self.assertTrue(len(LookAtImageTool.usage_prompt) > 200)


if __name__ == "__main__":
    unittest.main()

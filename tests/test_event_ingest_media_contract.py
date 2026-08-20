"""Contract for image media side effects in the EventIngest pipeline.

Static + monkeypatch-driven; no real network, no DB.

Verifies:
- Empty / non-image payloads are no-ops.
- Image with no URL/non-http URL/network failure → terminal media failure.
- Successful download → sha256 hashed, written under MEDIA_IMG_DIR/<hh>/<hash>,
  VLM-described, and enriched with file_hash/local_path/description metadata.
- Cross-scope dedup: identical content downloaded twice writes the file once.
- Concurrency: multiple images in one payload are fetched in parallel.
- EventIngest commits a preprocessing failure event instead of a half-ready message.
"""

from __future__ import annotations

import asyncio
import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import qqbot.services.event_ingest.media as media_mod
from qqbot.services.event_ingest import EventIngest, IngestFailureDetail
from qqbot.services.event_ingest.mappers import build_default_registry
from qqbot.services.event_ingest.media import (
    MEDIA_IMG_DIR,
    MediaProcessingResult,
    attach_media_to_payload,
)


def _img_seg(**data: Any) -> dict:
    return {"type": "image", "data": dict(data)}


async def _describe_ok(data: bytes, mime: str, file_hash: str) -> str:
    _ = data, mime, file_hash
    return "客观图片描述"


class _PatchedMedia:
    """Context manager: redirect MEDIA_IMG_DIR + monkeypatch _fetch."""

    def __init__(self, fake_fetch: Any, tmp_dir: Path) -> None:
        self._fake_fetch = fake_fetch
        self._tmp_dir = tmp_dir
        self._orig_fetch: Any = None
        self._orig_dir: Path | None = None

    def __enter__(self) -> "_PatchedMedia":
        self._orig_fetch = media_mod._fetch
        self._orig_dir = media_mod.MEDIA_IMG_DIR
        media_mod._fetch = self._fake_fetch
        media_mod.MEDIA_IMG_DIR = self._tmp_dir
        return self

    def __exit__(self, *args: Any) -> None:
        media_mod._fetch = self._orig_fetch
        media_mod.MEDIA_IMG_DIR = self._orig_dir  # type: ignore[assignment]


class AttachMediaShortCircuitsTests(unittest.IsolatedAsyncioTestCase):
    async def test_payload_without_segments_is_noop(self) -> None:
        payload: dict = {}
        result = await attach_media_to_payload(payload)
        self.assertEqual(payload, {})
        self.assertTrue(result.ok)

    async def test_empty_segments_is_noop(self) -> None:
        payload = {"segments": []}
        result = await attach_media_to_payload(payload)
        self.assertEqual(payload, {"segments": []})
        self.assertTrue(result.ok)

    async def test_no_image_segments_is_noop(self) -> None:
        payload = {"segments": [{"type": "text", "data": {"text": "hi"}}]}
        result = await attach_media_to_payload(payload)
        self.assertEqual(payload["segments"][0]["type"], "text")
        self.assertNotIn("downloaded", payload["segments"][0])
        self.assertTrue(result.ok)


class AttachMediaFailureModesTests(unittest.IsolatedAsyncioTestCase):
    async def test_image_without_url_marked_not_downloaded(self) -> None:
        payload = {"segments": [_img_seg()]}
        result = await attach_media_to_payload(payload)
        self.assertFalse(payload["segments"][0]["downloaded"])
        self.assertEqual(result.failures[0].error_code, "image_source_unavailable")

    async def test_image_with_non_http_url_marked_not_downloaded(self) -> None:
        payload = {"segments": [_img_seg(url="file:///local/x.jpg")]}
        result = await attach_media_to_payload(payload)
        seg = payload["segments"][0]
        self.assertFalse(seg["downloaded"])
        self.assertEqual(seg["original_url"], "file:///local/x.jpg")
        self.assertEqual(result.failures[0].stage, "image_download")

    async def test_download_exception_is_swallowed(self) -> None:
        async def boom(url: str) -> tuple[bytes, str]:
            raise RuntimeError("nope")

        with tempfile.TemporaryDirectory() as tmp:
            with _PatchedMedia(boom, Path(tmp)):
                payload = {"segments": [_img_seg(url="http://x/y.jpg")]}
                result = await attach_media_to_payload(payload)
        seg = payload["segments"][0]
        self.assertFalse(seg["downloaded"])
        self.assertEqual(seg["original_url"], "http://x/y.jpg")
        self.assertEqual(result.failures[0].error_code, "image_download_failed")

    async def test_multiple_images_collect_failures_into_one_result(self) -> None:
        payload = {
            "segments": [
                _img_seg(),
                _img_seg(url="file:///local/x.jpg"),
            ]
        }

        result = await attach_media_to_payload(payload)

        self.assertEqual(len(result.failures), 2)
        self.assertEqual(
            [failure.segment_index for failure in result.failures],
            [0, 1],
        )


class AttachMediaSuccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_download_enriches_segment_and_writes_file(self) -> None:
        content = b"\x89PNG\r\n\x1a\nfake"
        expected_hash = hashlib.sha256(content).hexdigest()

        async def ok(url: str) -> tuple[bytes, str]:
            return content, "image/png"

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            with _PatchedMedia(ok, tmp_dir):
                payload = {"segments": [_img_seg(url="http://x/y.png")]}
                result = await attach_media_to_payload(payload, _describe_ok)

                seg = payload["segments"][0]
                self.assertTrue(seg["downloaded"])
                self.assertEqual(seg["file_hash"], expected_hash)
                self.assertEqual(seg["mime"], "image/png")
                self.assertEqual(seg["byte_size"], len(content))
                self.assertEqual(seg["original_url"], "http://x/y.png")

                file_path = tmp_dir / expected_hash[:2] / expected_hash
                self.assertTrue(file_path.exists())
                self.assertEqual(file_path.read_bytes(), content)
                self.assertEqual(seg["local_path"], str(file_path))
                self.assertEqual(seg["description"], "客观图片描述")
                self.assertTrue(result.ok)


class ImageDescriptionTests(unittest.IsolatedAsyncioTestCase):
    """2026-07-28：下载成功的图再同步走一次 VLM 客观描述，写进
    seg["description"]（投影据此渲染 desc=）。生产实现在
    agent_loop/image_description.py，这里塞假 describer 全离线跑。"""

    @staticmethod
    async def _ok(url: str) -> tuple[bytes, str]:
        return b"\x89PNG\r\n\x1a\nfake", "image/png"

    async def test_describer_result_written_to_segment(self) -> None:
        seen: list[tuple[bytes, str, str]] = []

        async def describer(data: bytes, mime: str, file_hash: str) -> str:
            seen.append((data, mime, file_hash))
            return "一张终端截图"

        with tempfile.TemporaryDirectory() as tmp:
            with _PatchedMedia(self._ok, Path(tmp)):
                payload = {"segments": [_img_seg(url="http://x/y.png")]}
                await attach_media_to_payload(payload, describer)

        seg = payload["segments"][0]
        self.assertEqual(seg["description"], "一张终端截图")
        # describer 拿到的是原始 bytes + 嗅探到的 mime + 落盘用的 hash
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][1], "image/png")
        self.assertEqual(seen[0][2], seg["file_hash"])

    async def test_no_describer_is_a_terminal_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with _PatchedMedia(self._ok, Path(tmp)):
                payload = {"segments": [_img_seg(url="http://x/y.png")]}
                result = await attach_media_to_payload(payload)

        seg = payload["segments"][0]
        self.assertTrue(seg["downloaded"])
        self.assertNotIn("description", seg)
        self.assertEqual(
            result.failures[0].error_code,
            "image_describer_unavailable",
        )

    async def test_describer_exception_returns_terminal_failure(self) -> None:

        async def boom(data: bytes, mime: str, file_hash: str) -> str:
            raise RuntimeError("vlm gateway exploded")

        with tempfile.TemporaryDirectory() as tmp:
            with _PatchedMedia(self._ok, Path(tmp)):
                payload = {"segments": [_img_seg(url="http://x/y.png")]}
                result = await attach_media_to_payload(payload, boom)

        seg = payload["segments"][0]
        self.assertTrue(seg["downloaded"])
        self.assertNotIn("description", seg)
        self.assertEqual(
            result.failures[0].error_code,
            "image_description_failed",
        )

    async def test_empty_description_returns_terminal_failure(self) -> None:

        async def empty(data: bytes, mime: str, file_hash: str) -> None:
            return None

        with tempfile.TemporaryDirectory() as tmp:
            with _PatchedMedia(self._ok, Path(tmp)):
                payload = {"segments": [_img_seg(url="http://x/y.png")]}
                result = await attach_media_to_payload(payload, empty)

        self.assertNotIn("description", payload["segments"][0])
        self.assertEqual(
            result.failures[0].error_code,
            "image_description_empty",
        )

    async def test_failed_download_is_never_described(self) -> None:
        """下载失败的图不该浪费一次 VLM 调用 —— 根本没有 bytes 可看。"""
        called = {"n": 0}

        async def describer(data: bytes, mime: str, file_hash: str) -> str:
            called["n"] += 1
            return "never"

        async def fail(url: str) -> tuple[bytes, str]:
            raise RuntimeError("network down")

        with tempfile.TemporaryDirectory() as tmp:
            with _PatchedMedia(fail, Path(tmp)):
                payload = {"segments": [_img_seg(url="http://x/y.png")]}
                result = await attach_media_to_payload(payload, describer)

        self.assertFalse(payload["segments"][0]["downloaded"])
        self.assertEqual(called["n"], 0)
        self.assertEqual(result.failures[0].error_code, "image_download_failed")


class CrossScopeDedupTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_content_writes_file_once(self) -> None:
        content = b"identical"
        call_count = {"n": 0}

        async def ok(url: str) -> tuple[bytes, str]:
            call_count["n"] += 1
            return content, "image/jpeg"

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            with _PatchedMedia(ok, tmp_dir):
                payload_a = {"segments": [_img_seg(url="http://a/x.jpg")]}
                payload_b = {"segments": [_img_seg(url="http://b/x.jpg")]}
                await attach_media_to_payload(payload_a, _describe_ok)
                await attach_media_to_payload(payload_b, _describe_ok)

                expected_hash = hashlib.sha256(content).hexdigest()
                # 同 hash 共用一份本地文件；网络仍各下一次（无 URL 级缓存层）
                file_path = tmp_dir / expected_hash[:2] / expected_hash
                self.assertTrue(file_path.exists())
                self.assertEqual(call_count["n"], 2)
                self.assertEqual(
                    payload_a["segments"][0]["file_hash"],
                    payload_b["segments"][0]["file_hash"],
                )
                self.assertEqual(
                    payload_a["segments"][0]["local_path"],
                    payload_b["segments"][0]["local_path"],
                )


class ConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_multiple_images_fetch_in_parallel(self) -> None:
        order: list[str] = []

        async def slow(url: str) -> tuple[bytes, str]:
            order.append(f"start:{url}")
            await asyncio.sleep(0.05)
            order.append(f"end:{url}")
            return url.encode(), "image/png"

        with tempfile.TemporaryDirectory() as tmp:
            with _PatchedMedia(slow, Path(tmp)):
                payload = {
                    "segments": [
                        _img_seg(url=f"http://x/{i}.png") for i in range(3)
                    ]
                }
                result = await attach_media_to_payload(payload, _describe_ok)

        # 并行特征：所有 start 都在所有 end 之前出现
        starts = [i for i, ev in enumerate(order) if ev.startswith("start:")]
        ends = [i for i, ev in enumerate(order) if ev.startswith("end:")]
        self.assertLess(max(starts), min(ends))
        self.assertTrue(result.ok)


class MediaDefaultDirContractTests(unittest.TestCase):
    def test_default_dir_under_runtime_data(self) -> None:
        self.assertEqual(MEDIA_IMG_DIR, Path("./runtime_data/media/img"))


class IngestPipelineAttachesMediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_ingest_calls_attach_media_between_mapper_and_persist(self) -> None:
        seen: list[dict] = []
        attached_segs: list[dict] = []
        describers: list[Any] = []

        async def fake_attach(
            payload: dict,
            describer: Any = None,
            batch_describer: Any = None,
        ) -> MediaProcessingResult:
            _ = batch_describer
            seen.append(payload)
            describers.append(describer)
            # mimic side effect: mark image segs downloaded=true
            for seg in payload.get("segments", []):
                if isinstance(seg, dict) and seg.get("type") == "image":
                    seg["downloaded"] = True
                    seg["file_hash"] = "abc"
                    seg["description"] = "desc"
                    attached_segs.append(seg)
            return MediaProcessingResult()

        # monkeypatch attach_media_to_payload inside the ingest module
        import qqbot.services.event_ingest.ingest as ingest_mod

        original = ingest_mod.attach_media_to_payload
        ingest_mod.attach_media_to_payload = fake_attach
        try:
            captured: list[Any] = []

            class FakeSession:
                async def execute(self, stmt: Any) -> Any:
                    captured.append(stmt)
                    return SimpleNamespace(rowcount=1)

                async def commit(self) -> None:
                    return None

                async def __aenter__(self) -> "FakeSession":
                    return self

                async def __aexit__(self, *args: Any) -> None:
                    return None

            ingest = EventIngest(
                build_default_registry(), session_factory=FakeSession
            )
            event = SimpleNamespace(
                post_type="message",
                message_type="group",
                sub_type="normal",
                time=1716700000,
                self_id=10000,
                message_id=12345,
                group_id=999,
                user_id=222,
                raw_message="img",
                message=[SimpleNamespace(type="image", data={"url": "http://x/y.png"})],
                sender=SimpleNamespace(user_id=222, nickname="a"),
            )
            result = await ingest.ingest(event)
        finally:
            ingest_mod.attach_media_to_payload = original

        self.assertEqual(result.status, "inserted")
        # attach was invoked exactly once with the partial payload
        self.assertEqual(len(seen), 1)
        # the captured payload contained the image segment we mutated
        self.assertEqual(len(attached_segs), 1)
        self.assertTrue(attached_segs[0]["downloaded"])
        # 测试未注入 describer，因此 fake 收到 None；真实实现会把这种图片
        # 处理成 runtime.event_ingest_failed。
        self.assertEqual(describers, [None])

    async def test_media_failure_commits_only_terminal_failure_event(self) -> None:
        async def fake_attach(
            payload: dict,
            describer: Any = None,
            batch_describer: Any = None,
        ) -> MediaProcessingResult:
            _ = payload, describer, batch_describer
            return MediaProcessingResult(
                failures=(
                    IngestFailureDetail(
                        stage="image_description",
                        error_code="image_description_failed",
                        reason="图片描述生成失败",
                        segment_index=0,
                        segment_type="image",
                        file_hash="ab" * 32,
                    ),
                )
            )

        import qqbot.services.event_ingest.ingest as ingest_mod

        original = ingest_mod.attach_media_to_payload
        ingest_mod.attach_media_to_payload = fake_attach
        try:
            class FakeSession:
                async def execute(self, stmt: Any) -> Any:
                    return SimpleNamespace(rowcount=1)

                async def commit(self) -> None:
                    return None

                async def __aenter__(self) -> "FakeSession":
                    return self

                async def __aexit__(self, *args: Any) -> None:
                    return None

            committed: list[Any] = []

            async def notify(event: Any) -> None:
                committed.append(event)

            ingest = EventIngest(
                build_default_registry(),
                session_factory=FakeSession,
                committed_notifier=notify,
            )
            event = SimpleNamespace(
                post_type="message",
                message_type="group",
                sub_type="normal",
                time=1716700000,
                self_id=10000,
                message_id=12345,
                group_id=999,
                user_id=222,
                raw_message="帮我看看",
                message=[
                    SimpleNamespace(
                        type="image",
                        data={"url": "http://x/y.png"},
                    )
                ],
                sender=SimpleNamespace(user_id=222, nickname="a"),
            )
            result = await ingest.ingest(event)
        finally:
            ingest_mod.attach_media_to_payload = original

        self.assertEqual(result.status, "processing_failed")
        self.assertEqual(result.event.type, "runtime.event_ingest_failed")
        self.assertEqual(result.event.scope, "group")
        self.assertEqual(result.event.idempotency_key, "10000:msg:12345")
        self.assertEqual(result.event.payload["raw_message"], "帮我看看")
        self.assertEqual(
            result.event.payload["failures"][0]["error_code"],
            "image_description_failed",
        )
        self.assertEqual(committed, [result.event])

    async def test_heartbeat_skips_attach_media(self) -> None:
        called = MagicMock()

        async def fake_attach(
            payload: dict,
            describer: Any = None,
            batch_describer: Any = None,
        ) -> MediaProcessingResult:
            _ = payload, describer, batch_describer
            called()
            return MediaProcessingResult()

        import qqbot.services.event_ingest.ingest as ingest_mod

        original = ingest_mod.attach_media_to_payload
        ingest_mod.attach_media_to_payload = fake_attach
        try:
            with tempfile.TemporaryDirectory() as tmp:
                import qqbot.services.event_ingest.heartbeat as hb_mod

                orig_hb = hb_mod.HEARTBEAT_FILE
                hb_mod.HEARTBEAT_FILE = Path(tmp) / "heartbeat.json"
                try:
                    ingest = EventIngest(
                        build_default_registry(),
                        session_factory=MagicMock(
                            side_effect=AssertionError("session must not be used")
                        ),
                    )
                    event = SimpleNamespace(
                        post_type="meta_event",
                        meta_event_type="heartbeat",
                        time=1716700000,
                        self_id=10000,
                        interval=5000,
                        status={},
                    )
                    result = await ingest.ingest(event)
                finally:
                    hb_mod.HEARTBEAT_FILE = orig_hb
        finally:
            ingest_mod.attach_media_to_payload = original

        self.assertEqual(result.status, "heartbeat")
        called.assert_not_called()


if __name__ == "__main__":
    unittest.main()

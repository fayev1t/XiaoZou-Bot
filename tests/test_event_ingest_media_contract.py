"""Contract for image media side effects in the EventIngest pipeline.

Static + monkeypatch-driven; no real network, no DB.

Verifies:
- Empty / non-image payloads are no-ops.
- Image with no URL → marked downloaded=false.
- Image with non-http URL → marked downloaded=false (avoid following file://).
- Network failure → swallow + downloaded=false + original_url preserved.
- Successful download → sha256 hashed, written under MEDIA_IMG_DIR/<hh>/<hash>,
  segment enriched with file_hash / local_path / byte_size / mime / downloaded.
- Cross-scope dedup: identical content downloaded twice writes the file once.
- Concurrency: multiple images in one payload are fetched in parallel.
- EventIngest.ingest invokes attach_media_to_payload between mapper and persist.
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
from qqbot.services.event_ingest import EventIngest
from qqbot.services.event_ingest.mappers import build_default_registry
from qqbot.services.event_ingest.media import (
    MEDIA_IMG_DIR,
    attach_media_to_payload,
)


def _img_seg(**data: Any) -> dict:
    return {"type": "image", "data": dict(data)}


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
        await attach_media_to_payload(payload)
        self.assertEqual(payload, {})

    async def test_empty_segments_is_noop(self) -> None:
        payload = {"segments": []}
        await attach_media_to_payload(payload)
        self.assertEqual(payload, {"segments": []})

    async def test_no_image_segments_is_noop(self) -> None:
        payload = {"segments": [{"type": "text", "data": {"text": "hi"}}]}
        await attach_media_to_payload(payload)
        self.assertEqual(payload["segments"][0]["type"], "text")
        self.assertNotIn("downloaded", payload["segments"][0])


class AttachMediaFailureModesTests(unittest.IsolatedAsyncioTestCase):
    async def test_image_without_url_marked_not_downloaded(self) -> None:
        payload = {"segments": [_img_seg()]}
        await attach_media_to_payload(payload)
        self.assertFalse(payload["segments"][0]["downloaded"])

    async def test_image_with_non_http_url_marked_not_downloaded(self) -> None:
        payload = {"segments": [_img_seg(url="file:///local/x.jpg")]}
        await attach_media_to_payload(payload)
        seg = payload["segments"][0]
        self.assertFalse(seg["downloaded"])
        self.assertEqual(seg["original_url"], "file:///local/x.jpg")

    async def test_download_exception_is_swallowed(self) -> None:
        async def boom(url: str) -> tuple[bytes, str]:
            raise RuntimeError("nope")

        with tempfile.TemporaryDirectory() as tmp:
            with _PatchedMedia(boom, Path(tmp)):
                payload = {"segments": [_img_seg(url="http://x/y.jpg")]}
                await attach_media_to_payload(payload)
        seg = payload["segments"][0]
        self.assertFalse(seg["downloaded"])
        self.assertEqual(seg["original_url"], "http://x/y.jpg")


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
                await attach_media_to_payload(payload)

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

    async def test_no_describer_leaves_segment_without_description(self) -> None:
        """未注入 describer（早期骨架 / 既有测试）→ 图照常下载落盘，只是没有
        description 字段，行为与 2026-07-28 之前逐字节一致。"""
        with tempfile.TemporaryDirectory() as tmp:
            with _PatchedMedia(self._ok, Path(tmp)):
                payload = {"segments": [_img_seg(url="http://x/y.png")]}
                await attach_media_to_payload(payload)

        seg = payload["segments"][0]
        self.assertTrue(seg["downloaded"])
        self.assertNotIn("description", seg)

    async def test_describer_failure_never_breaks_ingest(self) -> None:
        """describer 抛异常 → 吞掉，图仍然算下载成功、只是没描述。
        "ingest never raises" 是硬约束，描述只是锦上添花。"""

        async def boom(data: bytes, mime: str, file_hash: str) -> str:
            raise RuntimeError("vlm gateway exploded")

        with tempfile.TemporaryDirectory() as tmp:
            with _PatchedMedia(self._ok, Path(tmp)):
                payload = {"segments": [_img_seg(url="http://x/y.png")]}
                await attach_media_to_payload(payload, boom)

        seg = payload["segments"][0]
        self.assertTrue(seg["downloaded"])
        self.assertNotIn("description", seg)

    async def test_empty_description_not_written(self) -> None:
        """describer 返回 None/空串 → 不写字段（投影据此省掉 desc= 属性）。"""

        async def empty(data: bytes, mime: str, file_hash: str) -> None:
            return None

        with tempfile.TemporaryDirectory() as tmp:
            with _PatchedMedia(self._ok, Path(tmp)):
                payload = {"segments": [_img_seg(url="http://x/y.png")]}
                await attach_media_to_payload(payload, empty)

        self.assertNotIn("description", payload["segments"][0])

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
                await attach_media_to_payload(payload, describer)

        self.assertFalse(payload["segments"][0]["downloaded"])
        self.assertEqual(called["n"], 0)


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
                await attach_media_to_payload(payload_a)
                await attach_media_to_payload(payload_b)

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
                await attach_media_to_payload(payload)

        # 并行特征：所有 start 都在所有 end 之前出现
        starts = [i for i, ev in enumerate(order) if ev.startswith("start:")]
        ends = [i for i, ev in enumerate(order) if ev.startswith("end:")]
        self.assertLess(max(starts), min(ends))


class MediaDefaultDirContractTests(unittest.TestCase):
    def test_default_dir_under_runtime_data(self) -> None:
        self.assertEqual(MEDIA_IMG_DIR, Path("./runtime_data/media/img"))


class IngestPipelineAttachesMediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_ingest_calls_attach_media_between_mapper_and_persist(self) -> None:
        seen: list[dict] = []
        attached_segs: list[dict] = []
        describers: list[Any] = []

        async def fake_attach(payload: dict, describer: Any = None) -> None:
            seen.append(payload)
            describers.append(describer)
            # mimic side effect: mark image segs downloaded=true
            for seg in payload.get("segments", []):
                if isinstance(seg, dict) and seg.get("type") == "image":
                    seg["downloaded"] = True
                    seg["file_hash"] = "abc"
                    attached_segs.append(seg)

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
        # 未注入 image_describer 的 EventIngest 透传 None（2026-07-28）：
        # 描述是可选注入，不注入时图片照常下载落盘、只是没有 desc。
        self.assertEqual(describers, [None])

    async def test_heartbeat_skips_attach_media(self) -> None:
        called = MagicMock()

        async def fake_attach(payload: dict, describer: Any = None) -> None:
            called()

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

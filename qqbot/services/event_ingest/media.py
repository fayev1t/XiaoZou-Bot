"""Required media preprocessing for the EventIngest pipeline.

EventIngest契约.md §5.1: image segments inside external.message.* payloads
are downloaded synchronously, sha256-hashed, and have their segment fields
enriched in place with file_hash / local_path / byte_size / mime / downloaded.

- Concurrency: all image segments in a single message download in parallel
  (`asyncio.gather`), so a 9-image album is bounded by one timeout window.
  Since 2026-07-28 the VLM description call rides the same gather, so an album
  costs one description round-trip in wall-clock, not N.
- Failure mode: every image reaches a terminal success/failure result. A failed
  download, local write, or VLM description prevents the normal message event
  from being committed; EventIngest converts the result into one
  ``runtime.event_ingest_failed`` internal event instead.
- Cross-scope dedup: files are addressed by sha256, not by scope. Same hash
  across multiple groups uses one local copy (隔离契约 §9.2 第 6 条).
- Layout: runtime_data/media/img/<hash[:2]>/<hash>, two-char bucket prefix
  to keep any single directory's entry count bounded.

Audio / video / file segments are intentionally NOT downloaded here
(EventIngest契约.md §5.2): they keep their NapCat metadata only until a future
contract explicitly makes more preprocessing mandatory.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from qqbot.core.logging import get_logger
from qqbot.services.event_ingest.failure import IngestFailureDetail

logger = get_logger(__name__)

MEDIA_IMG_DIR = Path("./runtime_data/media/img")
_DOWNLOAD_TIMEOUT_SECONDS = 5.0

# 看图写客观描述的回调（生产 = image_description.describe_image 绑定
# session_factory 后的偏函数，由 v2_main 注入 EventIngest 再传到这里）。
# 签名 async (bytes, mime, file_hash) -> str | None，失败自己吞成 None。
# 这里刻意不 import agent_loop —— ingest 不反向依赖 agent_loop；生产装配
# 只把回调注入 EventIngest，契约测试可塞假 describer 离线运行。
ImageDescriber = Callable[[bytes, str, str], Awaitable[str | None]]


@dataclass(frozen=True, slots=True)
class MediaProcessingResult:
    failures: tuple[IngestFailureDetail, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.failures


async def attach_media_to_payload(
    payload: dict[str, Any], describer: ImageDescriber | None = None
) -> MediaProcessingResult:
    """Download every image segment in payload.segments and enrich in place.

    describer 非 None 时，每张下载成功的图再同步走一次 VLM 客观描述，结果写进
    ``seg["description"]``（投影据此渲染 ``<image ... desc="..."/>``）。描述与
    下载在同一个 gather 里并发，相册不会串行叠加延迟。
    """
    segments = payload.get("segments")
    if not isinstance(segments, list) or not segments:
        return MediaProcessingResult()

    image_segs = [
        (index, seg)
        for index, seg in enumerate(segments)
        if isinstance(seg, dict) and seg.get("type") == "image"
    ]
    if not image_segs:
        return MediaProcessingResult()

    results = await asyncio.gather(
        *(
            _attach_image(index, segment, describer)
            for index, segment in image_segs
        ),
        return_exceptions=True,
    )
    failures: list[IngestFailureDetail] = []
    for (index, _), result in zip(image_segs, results, strict=True):
        if result is None:
            continue
        if isinstance(result, IngestFailureDetail):
            failures.append(result)
            continue
        if isinstance(result, asyncio.CancelledError):
            raise result
        logger.warning(
            "[media] unexpected image preprocessing failure: index={} err={}",
            index,
            result,
        )
        failures.append(
            IngestFailureDetail(
                stage="image_processing",
                error_code="image_processing_error",
                reason="图片前置处理发生内部错误",
                segment_index=index,
                segment_type="image",
            )
        )
    return MediaProcessingResult(failures=tuple(failures))


async def _attach_image(
    index: int,
    seg: dict[str, Any],
    describer: ImageDescriber | None = None,
) -> IngestFailureDetail | None:
    data = seg.get("data") or {}
    url = data.get("url") or data.get("file")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        seg["downloaded"] = False
        if isinstance(url, str) and url:
            seg["original_url"] = url
        return IngestFailureDetail(
            stage="image_download",
            error_code="image_source_unavailable",
            reason="图片缺少可下载的 HTTP 地址",
            segment_index=index,
            segment_type="image",
        )

    try:
        content, mime = await _fetch(url)
    except Exception as exc:
        logger.warning("[media] image download failed: {} url={}", exc, url)
        seg["downloaded"] = False
        seg["original_url"] = url
        return IngestFailureDetail(
            stage="image_download",
            error_code="image_download_failed",
            reason="图片下载失败",
            segment_index=index,
            segment_type="image",
        )

    file_hash = hashlib.sha256(content).hexdigest()
    path = MEDIA_IMG_DIR / file_hash[:2] / file_hash

    if not path.exists():
        try:
            await asyncio.to_thread(_atomic_write, path, content)
        except Exception as exc:
            logger.warning(
                "[media] image local write failed: {} hash={}", exc, file_hash
            )
            seg["downloaded"] = False
            seg["original_url"] = url
            return IngestFailureDetail(
                stage="image_persist",
                error_code="image_write_failed",
                reason="图片落盘失败",
                segment_index=index,
                segment_type="image",
                file_hash=file_hash,
            )

    seg["file_hash"] = file_hash
    seg["local_path"] = str(path)
    seg["original_url"] = url
    seg["downloaded"] = True
    seg["byte_size"] = len(content)
    seg["mime"] = mime

    if describer is None:
        return IngestFailureDetail(
            stage="image_description",
            error_code="image_describer_unavailable",
            reason="图片描述器未配置",
            segment_index=index,
            segment_type="image",
            file_hash=file_hash,
        )
    try:
        description = await describer(content, mime, file_hash)
    except Exception as exc:
        logger.warning(
            "[media] image description failed: {} hash={}", exc, file_hash
        )
        return IngestFailureDetail(
            stage="image_description",
            error_code="image_description_failed",
            reason="图片描述生成失败",
            segment_index=index,
            segment_type="image",
            file_hash=file_hash,
        )
    if not isinstance(description, str) or not description.strip():
        return IngestFailureDetail(
            stage="image_description",
            error_code="image_description_empty",
            reason="图片描述结果为空",
            segment_index=index,
            segment_type="image",
            file_hash=file_hash,
        )
    seg["description"] = description
    return None


async def _fetch(url: str) -> tuple[bytes, str]:
    """Default HTTP fetcher. Tests monkeypatch this symbol to inject fakes."""
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(_DOWNLOAD_TIMEOUT_SECONDS),
        follow_redirects=True,
    ) as client:
        response = await client.get(url)
        response.raise_for_status()
        mime = response.headers.get("content-type", "").split(";")[0].strip()
        return response.content, mime


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".img-", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


__all__ = [
    "MEDIA_IMG_DIR",
    "ImageDescriber",
    "MediaProcessingResult",
    "attach_media_to_payload",
]

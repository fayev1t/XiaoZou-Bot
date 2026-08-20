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
  across multiple groups uses one local copy (事件系统设计 §11.3).
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
# (bytes, mime, file_hash) 一组，返回等长描述列表。
BatchImageDescriber = Callable[
    [list[tuple[bytes, str, str]]], Awaitable[list[str | None]]
]
_VLM_BATCH_SIZE = 5


@dataclass(frozen=True, slots=True)
class MediaProcessingResult:
    failures: tuple[IngestFailureDetail, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.failures


async def attach_media_to_payload(
    payload: dict[str, Any],
    describer: ImageDescriber | None = None,
    batch_describer: BatchImageDescriber | None = None,
) -> MediaProcessingResult:
    """Download every image, then describe. 描述按每 5 张一次模型请求。"""
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
        *(_download_image(index, segment) for index, segment in image_segs),
        return_exceptions=True,
    )
    failures: list[IngestFailureDetail] = []
    pending: list[tuple[int, dict[str, Any], bytes, str, str]] = []
    for (index, segment), result in zip(image_segs, results, strict=True):
        if isinstance(result, IngestFailureDetail):
            failures.append(result)
            continue
        if isinstance(result, asyncio.CancelledError):
            raise result
        if isinstance(result, Exception):
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
            continue
        content, mime, file_hash = result
        pending.append((index, segment, content, mime, file_hash))

    if not pending:
        return MediaProcessingResult(failures=tuple(failures))

    describe_failures = await _describe_pending(
        pending,
        describer=describer,
        batch_describer=batch_describer,
    )
    failures.extend(describe_failures)
    return MediaProcessingResult(failures=tuple(failures))


async def _describe_pending(
    pending: list[tuple[int, dict[str, Any], bytes, str, str]],
    *,
    describer: ImageDescriber | None,
    batch_describer: BatchImageDescriber | None,
) -> list[IngestFailureDetail]:
    failures: list[IngestFailureDetail] = []
    if batch_describer is not None:
        for start in range(0, len(pending), _VLM_BATCH_SIZE):
            chunk = pending[start : start + _VLM_BATCH_SIZE]
            items = [
                (content, mime, file_hash)
                for _, _, content, mime, file_hash in chunk
            ]
            try:
                descriptions = await batch_describer(items)
            except Exception as exc:
                logger.warning("[media] batch description failed: {}", exc)
                descriptions = [None] * len(chunk)
            if len(descriptions) != len(chunk):
                descriptions = list(descriptions) + [None] * (
                    len(chunk) - len(descriptions)
                )
                descriptions = descriptions[: len(chunk)]
            for (index, segment, _, _, file_hash), description in zip(
                chunk, descriptions, strict=True
            ):
                failure = _apply_description(
                    index, segment, file_hash, description
                )
                if failure is not None:
                    failures.append(failure)
        return failures

    if describer is None:
        for index, segment, _, _, file_hash in pending:
            failures.append(
                IngestFailureDetail(
                    stage="image_description",
                    error_code="image_describer_unavailable",
                    reason="图片描述器未配置",
                    segment_index=index,
                    segment_type="image",
                    file_hash=file_hash,
                )
            )
        return failures

    results = await asyncio.gather(
        *(
            _describe_one(describer, content, mime, file_hash)
            for _, _, content, mime, file_hash in pending
        ),
        return_exceptions=True,
    )
    for (index, segment, _, _, file_hash), result in zip(
        pending, results, strict=True
    ):
        if isinstance(result, Exception):
            logger.warning(
                "[media] image description failed: {} hash={}", result, file_hash
            )
            failures.append(
                IngestFailureDetail(
                    stage="image_description",
                    error_code="image_description_failed",
                    reason="图片描述生成失败",
                    segment_index=index,
                    segment_type="image",
                    file_hash=file_hash,
                )
            )
            continue
        failure = _apply_description(index, segment, file_hash, result)
        if failure is not None:
            failures.append(failure)
    return failures


async def _describe_one(
    describer: ImageDescriber,
    content: bytes,
    mime: str,
    file_hash: str,
) -> str | None:
    return await describer(content, mime, file_hash)


def _apply_description(
    index: int,
    seg: dict[str, Any],
    file_hash: str,
    description: str | None,
) -> IngestFailureDetail | None:
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


async def _download_image(
    index: int,
    seg: dict[str, Any],
) -> tuple[bytes, str, str] | IngestFailureDetail:
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
    return content, mime, file_hash


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
    "BatchImageDescriber",
    "ImageDescriber",
    "MediaProcessingResult",
    "attach_media_to_payload",
]

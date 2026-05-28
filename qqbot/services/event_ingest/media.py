"""Media side effects for the EventIngest pipeline.

EventIngest契约.md §6.1: image segments inside external.message.* payloads
are downloaded synchronously, sha256-hashed, and have their segment fields
enriched in place with file_hash / local_path / byte_size / mime / downloaded.

- Concurrency: all image segments in a single message download in parallel
  (`asyncio.gather`), so a 9-image album is bounded by one timeout window.
- Failure mode: any download / write failure marks the segment
  `downloaded=false` and continues; the ingest never raises.
- Cross-scope dedup: files are addressed by sha256, not by scope. Same hash
  across multiple groups uses one local copy (隔离契约 §9.2 第 6 条).
- Layout: runtime_data/media/img/<hash[:2]>/<hash>, two-char bucket prefix
  to keep any single directory's entry count bounded.

Audio / video / file segments are intentionally NOT downloaded here
(EventIngest契约.md §6.3): they keep their napcat metadata only, and any
later transcription / preview happens via tool calls.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
from pathlib import Path

import httpx

from qqbot.core.logging import get_logger

logger = get_logger(__name__)

MEDIA_IMG_DIR = Path("./runtime_data/media/img")
_DOWNLOAD_TIMEOUT_SECONDS = 5.0


async def attach_media_to_payload(payload: dict) -> None:
    """Download every image segment in payload.segments and enrich in place."""
    segments = payload.get("segments")
    if not isinstance(segments, list) or not segments:
        return

    image_segs = [
        seg
        for seg in segments
        if isinstance(seg, dict) and seg.get("type") == "image"
    ]
    if not image_segs:
        return

    await asyncio.gather(
        *(_attach_image(seg) for seg in image_segs),
        return_exceptions=True,
    )


async def _attach_image(seg: dict) -> None:
    data = seg.get("data") or {}
    url = data.get("url") or data.get("file")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        seg["downloaded"] = False
        if isinstance(url, str) and url:
            seg["original_url"] = url
        return

    try:
        content, mime = await _fetch(url)
    except Exception as exc:
        logger.warning("[media] image download failed: {} url={}", exc, url)
        seg["downloaded"] = False
        seg["original_url"] = url
        return

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
            return

    seg["file_hash"] = file_hash
    seg["local_path"] = str(path)
    seg["original_url"] = url
    seg["downloaded"] = True
    seg["byte_size"] = len(content)
    seg["mime"] = mime


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

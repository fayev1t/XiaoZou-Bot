"""meme 工具的公共小件（与 _onebot_common 同定位）。

- **hash 校验**：image_hash 必须是 64 位十六进制 sha256（LLM 从 timeline 的
  <image hash="..."/> 或 <saved-memes> 的 <meme hash="..."/> 原样抄来），大小写
  归一为小写。非法 → invalid_arguments（返回失败 outcome，不 raise）。
- **磁盘定位**：复用 EventIngest 的内容寻址布局
  runtime_data/media/img/<hash[:2]>/<hash>（EventIngest契约.md §6.1）。布局的
  唯一权威是 event_ingest.media.MEDIA_IMG_DIR —— 这里 import 它而不是抄一份
  路径常量（跨包 import 先例：event_writer → event_ingest.persistence）。
- **mime 嗅探**：落盘文件按 hash 命名无扩展名，caption 调用拼 data URL 需要
  mime，从 magic bytes 现场嗅探（比回查事件 payload 可靠且零 IO）。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from qqbot.services.agent_loop.tool_registry import ToolOutcome
from qqbot.services.event_ingest.media import MEDIA_IMG_DIR

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def coerce_image_hash(
    value: Any,
) -> tuple[str | None, ToolOutcome | None]:
    """arguments.image_hash → 归一化的小写 sha256。成功 ``(hash, None)``；
    非法 ``(None, invalid_arguments)``（带 reason_code=bad_image_hash）。"""
    if not isinstance(value, str) or not value.strip():
        return None, ToolOutcome.failure(
            "invalid_arguments",
            "image_hash is required: the 64-char sha256 hex copied verbatim "
            'from an <image hash="..."/> tag or a <meme hash="..."/> entry',
            field="image_hash",
            reason_code="bad_image_hash",
            retryable=False,
            transient=False,
            user_fixable=True,
        )
    normalized = value.strip().lower()
    if not _SHA256_RE.fullmatch(normalized):
        return None, ToolOutcome.failure(
            "invalid_arguments",
            f"image_hash must be 64 hex chars (sha256), got {value!r}",
            field="image_hash",
            reason_code="bad_image_hash",
            retryable=False,
            transient=False,
            user_fixable=True,
        )
    return normalized, None


def media_path_for_hash(file_hash: str) -> Path:
    """sha256 → EventIngest 落盘路径（两字符桶前缀，media.py §Layout）。"""
    return MEDIA_IMG_DIR / file_hash[:2] / file_hash


def sniff_mime(data: bytes) -> str:
    """从 magic bytes 嗅探图片 mime；识别不出兜底 image/png（QQ 图片实际
    只会是 png/jpeg/gif/webp/bmp 之一，兜底值只影响 caption 的 data URL 标注，
    多模态后端普遍按内容自检，标错不致命）。"""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"BM"):
        return "image/bmp"
    return "image/png"

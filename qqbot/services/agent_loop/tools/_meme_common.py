"""meme 工具的公共小件（与 _onebot_common 同定位）。

- **hash 校验**：image_hash 是 12–64 位十六进制的 sha256 前缀（行文法 §7：
  信封只展示 12 位前缀，LLM 从时间线 ``<图 hash12 …>`` 段或收藏
  ``<meme>hash12 …`` 行原样抄来；完整 64 位仍被接受——旧行/旧文档引用不
  失效），大小写归一为小写。非法 → invalid_arguments（返回失败 outcome，
  不 raise）。
- **磁盘前缀解析**：``resolve_media_hash`` 把前缀解析为落盘的完整 hash
  （唯一匹配）；多义 → ambiguous_hash_prefix。
- **磁盘定位**：复用 EventIngest 的内容寻址布局
  runtime_data/media/img/<hash[:2]>/<hash>（EventIngest契约.md §5.1）。布局的
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

# 12 位下限 = 行文法 §7 的展示前缀长度（48 bit，碰撞概率可忽略）；64 位
# 上限 = 完整 sha256。中间长度同样接受（模型多抄了几位不该失败）。
_HASH_PREFIX_RE = re.compile(r"^[0-9a-f]{12,64}$")


def coerce_image_hash(
    value: Any,
) -> tuple[str | None, ToolOutcome | None]:
    """arguments.image_hash → 归一化的小写 hash 前缀（12–64 位 hex）。
    成功 ``(prefix, None)``；非法 ``(None, invalid_arguments)``
    （带 reason_code=bad_image_hash）。"""
    if not isinstance(value, str) or not value.strip():
        return None, ToolOutcome.failure(
            "invalid_arguments",
            "image_hash is required: copy the 12-char hash prefix verbatim "
            "from a <图 …> segment in the timeline or a <meme> entry in "
            "表情包收藏",
            field="image_hash",
            reason_code="bad_image_hash",
            retryable=False,
            transient=False,
            user_fixable=True,
        )
    normalized = value.strip().lower()
    if not _HASH_PREFIX_RE.fullmatch(normalized):
        return None, ToolOutcome.failure(
            "invalid_arguments",
            f"image_hash must be 12-64 hex chars (sha256 prefix), "
            f"got {value!r}",
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


def resolve_media_hash(
    hash_prefix: str,
) -> tuple[str | None, ToolOutcome | None]:
    """hash 前缀 → 磁盘上唯一匹配的完整 sha256（行文法 §7）。

    返回三态：
    - ``(完整hash, None)``：唯一命中（完整 64 位输入直接原样返回，存在性
      仍由调用方的读文件路径判定——与旧行为逐字节兼容）；
    - ``(None, None)``：无命中，调用方渲染自己的 not-found 失败；
    - ``(None, failure)``：多条命中 → invalid_arguments
      （reason_code=ambiguous_hash_prefix，几乎不可能，语义封死）。

    前缀 ≥12 位恒包含两字符桶目录名，桶内 glob 即可；目录不存在等价无命中。
    """
    if len(hash_prefix) == 64:
        return hash_prefix, None
    bucket = MEDIA_IMG_DIR / hash_prefix[:2]
    try:
        matches = sorted(
            p.name
            for p in bucket.glob(f"{hash_prefix}*")
            if _HASH_PREFIX_RE.fullmatch(p.name) and len(p.name) == 64
        )
    except OSError:
        matches = []
    if not matches:
        return None, None
    if len(matches) > 1:
        return None, ToolOutcome.failure(
            "invalid_arguments",
            f"hash prefix {hash_prefix} matches {len(matches)} images; "
            "copy more characters of the hash to disambiguate",
            field="image_hash",
            reason_code="ambiguous_hash_prefix",
            retryable=False,
            transient=False,
            user_fixable=True,
        )
    return matches[0], None


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

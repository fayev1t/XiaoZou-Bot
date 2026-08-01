"""出站消息的共享校验、归一、准备、发送与回执整形。

2026-07-31 删除 Replyer 时从三处迁出（重构提案-删除Replyer.md §4.2）：

- ``validate_content`` / ``invalid_args``：原 ``tools/send_message.py`` 的
  ``_validate_content``（该工具随校验迁出后删除）。OneBot 段白名单 + 结构 +
  顺序 + 每段字段的前置校验，不把非法段推给上游碰运气。
- ``normalize_segment``：原 ``replyer.py`` 的 LLM 输出段格式归一表。新模型
  （尤其 Gemini 系，2026-07-22 快照实证）常把 OneBot 段拍平成
  ``{"type":"text","text":...}``，或把 reply 的 id 写成 message_id。只做
  无损归一，其余形态原样透传交严格校验 fail loudly。
- ``preflight_memes`` / ``send_all`` / ``delivery_status`` 与回执整形：原
  ``reply_executor.py`` 的 meme 准备（收藏核验 + 媒体读取 + base64）、
  OneBot 逐条发送与逐条回执（sent / failed / uncertain 三态 +
  base64/二进制脱敏）。删除的只是 Replyer 的 LLM 调用链，不重写已经工作的
  校验与发送逻辑。

消费者：``tools/send_messages.py``（Planner 直接发言的唯一出口）。
"""

from __future__ import annotations

import base64
from typing import Any

from qqbot.services.agent_loop.tool_registry import ToolOutcome

# 一次发送最多 10 个气泡；meme 气泡不再单独限量（2026-07-31 放开，多少条、
# 几张图由提示词层的分寸把关，代码只兜住总条数这个硬上限）。
MAX_OUTBOUND_MESSAGES = 10

# 当前允许的出站段类型（白名单）。其它一律 unsupported_segment_type——不放行
# 给上游碰运气，给 LLM 精确的 segment_index/segment_type 而非一段 napcat
# 中文 wording。
_ALLOWED_SEGMENT_TYPES = frozenset({"text", "at", "reply", "face"})

# LLM 输出边界的段格式归一表：把拍平段无损还原成 OneBot data 包装。
_FLAT_SEGMENT_KEYS: dict[str, tuple[str, ...]] = {
    "text": ("text",),
    "at": ("qq",),
    "reply": ("id", "message_id"),
    "face": ("id",),
}


def invalid_args(
    reason_code: str,
    message: str,
    *,
    field: str = "messages",
    segment_index: int | None = None,
    segment_type: Any = None,
    user_fixable: bool = True,
) -> ToolOutcome:
    """构造结构类 ``invalid_arguments`` 失败（带弱语义字段）。

    retryable/transient 恒 False（重发同样参数必再失败）；user_fixable 默认
    True（改参数即可）。segment_index/segment_type 仅在段级错误时带上。
    """
    extra: dict[str, Any] = {
        "field": field,
        "reason_code": reason_code,
        "retryable": False,
        "transient": False,
        "user_fixable": user_fixable,
    }
    if segment_index is not None:
        extra["segment_index"] = segment_index
    if segment_type is not None:
        extra["segment_type"] = segment_type
    return ToolOutcome.failure("invalid_arguments", message, **extra)


def normalize_segment(segment: Any) -> Any:
    """已知漂移的无损归一；其余形态原样透传给严格校验。"""
    if not isinstance(segment, dict):
        return segment
    seg_type = segment.get("type")
    if not isinstance(seg_type, str):
        return segment
    flat_keys = _FLAT_SEGMENT_KEYS.get(seg_type)
    if flat_keys is None:
        return segment
    data = segment.get("data")
    if isinstance(data, dict):
        data = dict(data)
    else:
        data = {key: segment[key] for key in flat_keys if key in segment}
        if not data:
            return segment
    if seg_type == "reply" and "id" not in data and "message_id" in data:
        data["id"] = data.pop("message_id")
    return {"type": seg_type, "data": data}


def validate_content(content: Any) -> ToolOutcome | None:
    """校验一个气泡的 content 段数组。返回 None=通过，否则失败 outcome。

    - 非空数组；至少一个可见负载（不能只有空白文本）
    - 只放行 text/at/reply/face；其它段 → unsupported_segment_type
    - reply 段至多一个、且必须在 content[0]
    - 每段字段逐个校验
    """
    if not isinstance(content, list) or not content:
        return invalid_args(
            "content_empty", "chat content must be a non-empty list"
        )
    reply_count = 0
    has_visible = False
    for i, seg in enumerate(content):
        if not isinstance(seg, dict):
            return invalid_args(
                "bad_field_type", f"content[{i}] must be an object", segment_index=i
            )
        seg_type = seg.get("type")
        if seg_type not in _ALLOWED_SEGMENT_TYPES:
            return invalid_args(
                "unsupported_segment_type",
                f"content[{i}].type={seg_type!r} is not supported; "
                "allowed: text/at/reply/face",
                segment_index=i,
                segment_type=str(seg_type) if seg_type is not None else None,
            )
        data = seg.get("data")
        if not isinstance(data, dict):
            return invalid_args(
                "bad_field_type",
                f"content[{i}].data must be an object",
                segment_index=i,
                segment_type=seg_type,
            )
        if seg_type == "text":
            text = data.get("text")
            if not isinstance(text, str):
                return invalid_args(
                    "bad_field_type",
                    f"content[{i}].data.text must be a string",
                    segment_index=i,
                    segment_type="text",
                )
            if text.strip():
                has_visible = True
        elif seg_type == "at":
            if fail := _validate_at(data, i):
                return fail
            has_visible = True
        elif seg_type == "reply":
            reply_count += 1
            if reply_count > 1:
                return invalid_args(
                    "duplicate_reply_segment",
                    "at most one reply segment is allowed",
                    segment_index=i,
                    segment_type="reply",
                )
            if i != 0:
                return invalid_args(
                    "reply_segment_not_first",
                    "the reply segment must be content[0]",
                    segment_index=i,
                    segment_type="reply",
                )
            rid = data.get("id")
            if rid is None or not str(rid).strip():
                return invalid_args(
                    "missing_required_field",
                    f"content[{i}] reply segment requires a non-empty data.id",
                    segment_index=i,
                    segment_type="reply",
                )
            has_visible = True
        elif seg_type == "face":
            fid = data.get("id")
            if fid is None or not str(fid).strip():
                return invalid_args(
                    "missing_required_field",
                    f"content[{i}] face segment requires a non-empty data.id",
                    segment_index=i,
                    segment_type="face",
                )
            has_visible = True
    if not has_visible:
        return invalid_args(
            "content_all_blank",
            "chat content has no visible payload (only blank text)",
        )
    return None


def _validate_at(data: dict, i: int) -> ToolOutcome | None:
    """at 段：data.qq 是 string/number；``"all"``（@全体）或可归一化为正整数。"""
    qq = data.get("qq")
    if isinstance(qq, bool) or not isinstance(qq, (str, int)):
        return invalid_args(
            "bad_field_type",
            f"content[{i}].data.qq must be a string or number",
            segment_index=i,
            segment_type="at",
        )
    qs = str(qq).strip()
    if qs == "all":
        return None
    if not qs.isdigit() or int(qs) <= 0:
        return invalid_args(
            "bad_field_type",
            f"content[{i}].data.qq must be 'all' or a positive QQ id, got {qq!r}",
            segment_index=i,
            segment_type="at",
        )
    return None


def _is_sha256_hex(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(c in "0123456789abcdef" for c in value.lower())
    )


def validate_messages(
    messages: Any,
) -> tuple[list[dict], ToolOutcome | None]:
    """出站 ``messages`` 数组的静态校验 + 归一。

    返回 ``(归一后的 messages, None)`` 或 ``([], 失败 outcome)``。结构规则：
    1–``MAX_OUTBOUND_MESSAGES`` 个气泡；每个是 ``{"kind":"chat","content":[…]}``
    或 ``{"kind":"meme","image_hash":"<sha256>"}``；chat 段先经
    ``normalize_segment`` 归一再走严格校验；meme 气泡数量不限（hash 是否仍在
    收藏、媒体是否可读属于投递前的动态 preflight，不在这里查库）。
    """
    if not isinstance(messages, list) or not messages:
        return [], invalid_args(
            "messages_empty",
            "messages must be a non-empty array; if there is nothing to say, "
            "do not call this tool",
        )
    if len(messages) > MAX_OUTBOUND_MESSAGES:
        return [], invalid_args(
            "too_many_messages",
            f"at most {MAX_OUTBOUND_MESSAGES} bubbles per send",
        )
    normalized: list[dict] = []
    for index, item in enumerate(messages):
        if not isinstance(item, dict):
            return [], invalid_args(
                "bad_field_type", f"messages[{index}] must be an object"
            )
        kind = item.get("kind")
        if kind == "chat":
            extras = sorted(set(item) - {"kind", "content"})
            if extras:
                return [], invalid_args(
                    "unexpected_argument",
                    f"messages[{index}] has unknown key(s): {', '.join(extras)}",
                )
            content = item.get("content")
            if not isinstance(content, list):
                return [], invalid_args(
                    "bad_field_type",
                    f"messages[{index}].content must be an array",
                )
            content = [normalize_segment(seg) for seg in content]
            if fail := validate_content(content):
                return [], fail
            normalized.append({"kind": "chat", "content": content})
        elif kind == "meme":
            extras = sorted(set(item) - {"kind", "image_hash"})
            if extras:
                return [], invalid_args(
                    "unexpected_argument",
                    f"messages[{index}] has unknown key(s): {', '.join(extras)}",
                )
            image_hash = item.get("image_hash")
            if not _is_sha256_hex(image_hash):
                return [], invalid_args(
                    "bad_image_hash",
                    f"messages[{index}].image_hash must be a 64-char sha256 "
                    "hex copied from <saved-memes>",
                )
            normalized.append(
                {"kind": "meme", "image_hash": str(image_hash).lower()}
            )
        else:
            return [], invalid_args(
                "bad_message_kind",
                f'messages[{index}].kind must be "chat" or "meme"',
            )
    return normalized, None


def extract_message_id(result: Any) -> Any:
    if isinstance(result, dict):
        return result.get("message_id")
    if isinstance(result, int):
        return result
    return None


async def preflight_memes(
    session_factory: Any, prepared: list[dict]
) -> tuple[list[dict], tuple[str, str] | None]:
    """投递前核验并装载 meme 气泡：仍在收藏、媒体可读，读入字节备发送。

    静态校验之外可能变化的外部事实在这里查；失败返回
    ``([], (reason_code, message))``，一条都不发。
    """
    from qqbot.services.agent_loop.meme_store import get_meme
    from qqbot.services.agent_loop.tools._meme_common import media_path_for_hash

    loaded: list[dict] = []
    for index, item in enumerate(prepared):
        if item["kind"] != "meme":
            loaded.append(item)
            continue
        image_hash = item["image_hash"]
        meme = await get_meme(session_factory, image_hash)
        if meme is None:
            return [], (
                "meme_not_saved",
                f"messages[{index}] meme is no longer saved",
            )
        try:
            data = media_path_for_hash(image_hash).read_bytes()
        except OSError as exc:
            return [], (
                "meme_media_missing",
                f"messages[{index}] meme media missing: {exc}",
            )
        loaded.append({"kind": "meme", "image_hash": image_hash, "data": data})
    return loaded, None


async def send_all(bot: Any, scope_key: str, prepared: list[dict]) -> list[dict]:
    """OneBot 逐条发送并生成回执；单条失败/存疑不阻断后续气泡。

    回执三态：``sent``（拿到 message_id）/ ``failed``（napcat 明确拒绝）/
    ``uncertain``（传输中断或 ok 无 message_id——可能已发出）。
    """
    from qqbot.services.agent_loop.event_writer import parse_scope_key
    from qqbot.services.agent_loop.tools._onebot_common import call_action

    _, group_id, _ = parse_scope_key(scope_key)
    receipts: list[dict] = []
    for index, item in enumerate(prepared):
        if item["kind"] == "chat":
            content = item["content"]
        else:
            content = [
                {
                    "type": "image",
                    "data": {
                        "file": "base64://"
                        + base64.b64encode(item["data"]).decode("ascii")
                    },
                }
            ]
        public_item = {k: v for k, v in item.items() if k != "data"}
        try:
            result, action_fail = await call_action(
                bot,
                "send_group_msg",
                group_id=int(group_id),
                message=content,
            )
        except Exception as exc:
            receipts.append(
                uncertain_receipt(
                    index,
                    public_item,
                    "upstream_delivery_uncertain",
                    f"{type(exc).__name__}: {exc}"[:500],
                )
            )
            continue
        if action_fail:
            receipts.append(
                failed_receipt(
                    index,
                    public_item,
                    action_fail.error_kind,
                    action_fail.error_message,
                    action_fail.extra,
                )
            )
            continue
        message_id = extract_message_id(result)
        if message_id is None:
            receipts.append(
                uncertain_receipt(
                    index,
                    public_item,
                    "missing_message_id",
                    "upstream returned ok but no message_id",
                    result,
                )
            )
            continue
        receipts.append(
            {
                "index": index,
                **public_item,
                "status": "sent",
                "message_id": message_id,
                "self_id": str(getattr(bot, "self_id", "") or "") or None,
                "receipt": public_receipt(result),
            }
        )
    return receipts


def first_error_reason(receipts: list[dict]) -> str | None:
    for item in receipts:
        if item.get("status") == "sent":
            continue
        error = item.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if message:
                return str(message)[:500]
    return None


def delivery_status(receipts: list[dict]) -> str:
    """逐条回执折成整体 status：sent / partial / uncertain / failed。"""
    statuses = [item.get("status") for item in receipts]
    if statuses and all(status == "sent" for status in statuses):
        return "sent"
    if any(status == "sent" for status in statuses):
        return "partial"
    if any(status == "uncertain" for status in statuses):
        return "uncertain"
    return "failed"


def failed_receipt(
    index: int,
    item: dict,
    error_kind: str | None,
    error_message: str | None,
    extra: dict | None = None,
) -> dict:
    return {
        "index": index,
        **{k: v for k, v in item.items() if k != "data"},
        "status": "failed",
        "error": {
            "kind": error_kind,
            "message": redact_runtime_value(error_message),
            **public_receipt(extra),
        },
    }


def uncertain_receipt(
    index: int,
    item: dict,
    error_kind: str,
    error_message: str,
    receipt: Any = None,
) -> dict:
    return {
        "index": index,
        **{k: v for k, v in item.items() if k != "data"},
        "status": "uncertain",
        "receipt": public_receipt(receipt),
        "error": {
            "kind": error_kind,
            "message": redact_runtime_value(error_message),
        },
    }


def public_receipt(value: Any) -> dict:
    """保留可审计回执，但禁止 OneBot 回显的图片正文进入事件流。"""
    if not isinstance(value, dict):
        return {}
    return {str(key): redact_runtime_value(item) for key, item in value.items()}


def redact_runtime_value(value: Any) -> Any:
    if isinstance(value, str):
        if "base64://" in value:
            return "<base64-redacted>"
        return value
    if isinstance(value, bytes):
        return "<binary-redacted>"
    if isinstance(value, dict):
        return {
            str(key): redact_runtime_value(item) for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_runtime_value(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)

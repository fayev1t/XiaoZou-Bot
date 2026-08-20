"""出站消息的共享校验、归一、准备、发送与回执整形。

2026-07-31 删除 Replyer 时从三处迁出（v2.0/30-工具设计/发言链路设计.md §7）：

- ``validate_content`` / ``invalid_args``：原 ``tools/send_message.py`` 的
  ``_validate_content``（该工具随校验迁出后删除）。OneBot 段白名单 + 结构 +
  顺序 + 每段字段的前置校验，不把非法段推给上游碰运气。
- ``normalize_segment``：原 ``replyer.py`` 的 LLM 输出段格式归一表。新模型
  （尤其 Gemini 系，2026-07-22 快照实证）常把 OneBot 段拍平成
  ``{"type":"text","text":...}``，或把 reply 的 id 写成 message_id。只做
  无损归一，其余形态原样透传交严格校验 fail loudly。
- ``preflight_memes`` / ``send_all`` / ``delivery_status`` 与回执整形：原
  ``reply_executor.py`` 的 meme 准备（收藏核验 + 媒体读取 + base64）、
  经统一 OneBotGateway 逐条发送与逐条回执（sent / failed / uncertain 三态 +
  base64/二进制脱敏）。删除的只是 Replyer 的 LLM 调用链，不重写已经工作的
  校验与发送逻辑。

2026-08-14 出站气泡去协议化（v2.0/30-工具设计/发言链路设计.md §2.1）。此前
一个 chat 气泡是 ``{"kind":"chat","content":[{"type":"text","data":{"text":…}}]}``：
``data`` 包装、``type`` 判别、段顺序规则（reply 必须 content[0]）全部是 OneBot 11
的协议知识，与"说一句话"无关，却要模型每次发言都正确复述一遍。现在气泡是领域
形状，一项就是一条消息：

- chat：``{"text": "…"}``，可选 ``reply`` / ``at`` / ``face``
- meme：``{"meme": "<12 位 hash 前缀>"}``

OneBot 段数组由 ``build_chat_content`` 在发送时构造，顺序固定
reply → at → text → face。**出站侧的协议知识收敛到这一个函数**。

代价照实记：一个气泡内不能再把 ``@`` 或表情插在文字中间——``at`` 一律在文字前、
``face`` 一律在文字后。要精细排版就拆成两个气泡。

``normalize_segment`` / ``validate_content`` 不删，改为只服务
``_legacy_bubble_to_domain``：模型带着旧习惯写出 ``kind``/``content`` 时无损转成
新形状，不让一次形状迁移变成线上发不出话。该兼容路径不写进 usage 文档。

消费者：``tools/send_messages.py``（Planner 直接发言的唯一出口）。
"""

from __future__ import annotations

import asyncio
import base64
from typing import Any

from qqbot.services.agent_loop.program_api.onebot_gateway import RawOneBotResponse
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


def _is_hash_prefix(value: Any) -> bool:
    """12–64 位十六进制的 sha256 前缀（Part 3 §2.2：信封展示 12 位前缀，
    完整 64 位仍接受）。"""
    return (
        isinstance(value, str)
        and 12 <= len(value) <= 64
        and all(c in "0123456789abcdef" for c in value.lower())
    )


_CHAT_BUBBLE_KEYS = frozenset({"text", "reply", "at", "face"})


def _meme_bubble(index: int, image_hash: Any) -> tuple[dict, ToolOutcome | None]:
    if not _is_hash_prefix(image_hash):
        return {}, invalid_args(
            "bad_image_hash",
            f"messages[{index}].meme must be a 12-64 char sha256 hex prefix "
            "copied from a <meme> entry in 表情包收藏",
        )
    return {"kind": "meme", "image_hash": str(image_hash).lower()}, None


def _id_list(
    index: int, field: str, value: Any, reason_code: str, *, allow_all: bool
) -> tuple[list[str], ToolOutcome | None]:
    """``at`` / ``face`` 的单值或数组 → 归一后的字符串列表。

    单值写法（``"at": 10001``）是常态，数组是需要 @ 多个人时的写法；两种都收，
    内部统一成列表，调用方不必分情况。
    """
    raw = value if isinstance(value, list) else [value]
    if not raw:
        return [], None
    out: list[str] = []
    for entry in raw:
        if isinstance(entry, bool) or not isinstance(entry, (str, int)):
            return [], invalid_args(
                reason_code,
                f"messages[{index}].{field} must be a QQ id or a list of them",
            )
        text = str(entry).strip()
        if allow_all and text == "all":
            out.append("all")
            continue
        if not text.isdigit() or int(text) <= 0:
            hint = "'all' or a positive id" if allow_all else "a positive id"
            return [], invalid_args(
                reason_code,
                f"messages[{index}].{field} entries must be {hint}, "
                f"got {entry!r}",
            )
        out.append(text)
    return out, None


def _chat_bubble(index: int, item: dict) -> tuple[dict, ToolOutcome | None]:
    """领域形状的 chat 气泡 → 归一后的内部条目。

    ``text`` 是常规负载，但不强制：只 ``@`` 某人或只发一个 QQ 表情都是完整
    气泡。四个键全空才是 ``bubble_all_blank``。
    """
    extras = sorted(set(item) - _CHAT_BUBBLE_KEYS)
    if extras:
        return {}, invalid_args(
            "unexpected_argument",
            f"messages[{index}] has unknown key(s): {', '.join(extras)}; "
            f"allowed: {', '.join(sorted(_CHAT_BUBBLE_KEYS))}, meme",
        )

    text = item.get("text", "")
    if not isinstance(text, str):
        return {}, invalid_args(
            "bad_field_type", f"messages[{index}].text must be a string"
        )

    bubble: dict[str, Any] = {"kind": "chat", "text": text}

    if "reply" in item:
        reply = item["reply"]
        if isinstance(reply, bool) or not isinstance(reply, (str, int)):
            return {}, invalid_args(
                "bad_reply_target",
                f"messages[{index}].reply must be a message id copied from a "
                "#消息ID mark in the timeline",
            )
        reply_id = str(reply).strip()
        if not reply_id:
            return {}, invalid_args(
                "bad_reply_target",
                f"messages[{index}].reply must not be empty",
            )
        bubble["reply"] = reply_id

    at: list[str] = []
    if "at" in item:
        at, fail = _id_list(index, "at", item["at"], "bad_at_target", allow_all=True)
        if fail is not None:
            return {}, fail
        if at:
            bubble["at"] = at

    face: list[str] = []
    if "face" in item:
        face, fail = _id_list(
            index, "face", item["face"], "bad_face_id", allow_all=False
        )
        if fail is not None:
            return {}, fail
        if face:
            bubble["face"] = face

    if not text.strip() and not at and not face:
        return {}, invalid_args(
            "bubble_all_blank",
            f"messages[{index}] has no visible payload; give it text, at or face",
        )
    return bubble, None


def _legacy_bubble_to_domain(
    index: int, item: dict
) -> tuple[dict, ToolOutcome | None]:
    """旧 OneBot 形状（``kind``/``content``/段数组）→ 新领域气泡，无损。

    2026-08-14 形状迁移的兼容路径，与 ``normalize_segment`` 同性质：只接住模型
    的旧习惯，不写进 usage 文档，也不是可依赖的第二套输入契约。段顺序在转换中
    被折叠成 reply → at → text → face，与 ``build_chat_content`` 的输出顺序一致。
    """
    kind = item.get("kind")
    if kind == "meme":
        extras = sorted(set(item) - {"kind", "image_hash"})
        if extras:
            return {}, invalid_args(
                "unexpected_argument",
                f"messages[{index}] has unknown key(s): {', '.join(extras)}",
            )
        return _meme_bubble(index, item.get("image_hash"))
    if kind != "chat":
        return {}, invalid_args(
            "bad_message_kind",
            f"messages[{index}] is not a recognized bubble; use "
            '{"text": …} or {"meme": …}',
        )
    extras = sorted(set(item) - {"kind", "content"})
    if extras:
        return {}, invalid_args(
            "unexpected_argument",
            f"messages[{index}] has unknown key(s): {', '.join(extras)}",
        )
    content = item.get("content")
    if not isinstance(content, list):
        return {}, invalid_args(
            "bad_field_type", f"messages[{index}].content must be an array"
        )
    content = [normalize_segment(seg) for seg in content]
    if fail := validate_content(content):
        return {}, fail
    bubble: dict[str, Any] = {"kind": "chat", "text": ""}
    at: list[str] = []
    face: list[str] = []
    for seg in content:
        data = seg.get("data") or {}
        seg_type = seg.get("type")
        if seg_type == "text":
            bubble["text"] += str(data.get("text", ""))
        elif seg_type == "at":
            at.append(str(data.get("qq", "")).strip())
        elif seg_type == "reply":
            bubble["reply"] = str(data.get("id", "")).strip()
        elif seg_type == "face":
            face.append(str(data.get("id", "")).strip())
    if at:
        bubble["at"] = at
    if face:
        bubble["face"] = face
    return bubble, None


def validate_messages(
    messages: Any,
) -> tuple[list[dict], ToolOutcome | None]:
    """出站 ``messages`` 数组的静态校验 + 归一。

    返回 ``(归一后的 messages, None)`` 或 ``([], 失败 outcome)``。结构规则：
    1–``MAX_OUTBOUND_MESSAGES`` 个气泡；每项是 ``{"meme": "<hash>"}`` 或一个
    chat 气泡 ``{"text": …}``（可选 ``reply`` / ``at`` / ``face``）。meme 气泡
    数量不限——hash 是否仍在收藏、媒体是否可读属于投递前的动态 preflight，不在
    这里查库。

    带 ``kind`` 键的旧 OneBot 形状走 ``_legacy_bubble_to_domain`` 无损转换。
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
        if "kind" in item:
            bubble, fail = _legacy_bubble_to_domain(index, item)
        elif "meme" in item:
            extras = sorted(set(item) - {"meme"})
            if extras:
                return [], invalid_args(
                    "unexpected_argument",
                    f"messages[{index}] is a meme bubble and cannot carry "
                    f"{', '.join(extras)}; send text as its own bubble",
                )
            bubble, fail = _meme_bubble(index, item["meme"])
        else:
            bubble, fail = _chat_bubble(index, item)
        if fail is not None:
            return [], fail
        normalized.append(bubble)
    return normalized, None


def build_chat_content(bubble: dict) -> list[dict]:
    """归一后的 chat 气泡 → OneBot V11 段数组。**出站协议知识只在这里。**

    顺序固定 reply → at → text → face：reply 段必须是 content[0] 是 OneBot 的
    规则，模型不再需要知道它。
    """
    content: list[dict] = []
    reply = bubble.get("reply")
    if reply:
        content.append({"type": "reply", "data": {"id": str(reply)}})
    for qq in bubble.get("at") or []:
        content.append({"type": "at", "data": {"qq": str(qq)}})
    text = bubble.get("text") or ""
    if text:
        content.append({"type": "text", "data": {"text": text}})
    for face_id in bubble.get("face") or []:
        content.append({"type": "face", "data": {"id": str(face_id)}})
    return content


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

    hash 按前缀唯一匹配（Part 3 §2.2）：命中后把气泡的 ``image_hash`` 归一为
    收藏记录的**完整** 64 位——回执、事件载荷与 from_self 作者索引继续以
    完整 hash 为准，只有信封展示是 12 位前缀。
    """
    from qqbot.services.agent_loop.meme_store import find_meme_by_prefix
    from qqbot.services.agent_loop.tools._meme_common import media_path_for_hash

    loaded: list[dict] = []
    for index, item in enumerate(prepared):
        if item["kind"] != "meme":
            loaded.append(item)
            continue
        meme, ambiguous = await find_meme_by_prefix(
            session_factory, item["image_hash"]
        )
        if ambiguous:
            return [], (
                "ambiguous_hash_prefix",
                f"messages[{index}] hash prefix matches more than one saved "
                "meme; copy more characters to disambiguate",
            )
        if meme is None:
            return [], (
                "meme_not_saved",
                f"messages[{index}] meme is no longer saved",
            )
        image_hash = meme.file_hash
        try:
            data = media_path_for_hash(image_hash).read_bytes()
        except OSError as exc:
            return [], (
                "meme_media_missing",
                f"messages[{index}] meme media missing: {exc}",
            )
        loaded.append({"kind": "meme", "image_hash": image_hash, "data": data})
    return loaded, None


_send_locks: dict[str, asyncio.Lock] = {}


def send_lock(scope_key: str) -> asyncio.Lock:
    """同 scope 出站的进程内互斥（2026-08-14，程序拍间并行）。

    拍间并行后同一 scope 可能有两段程序同时跑到 ``send_all``；没有这把锁，
    A 的气泡 [1,2,3] 与 B 的 [x,y] 会在群里交错成 1,x,2,y,3。

    它**只**保证一次调用的气泡连续，不是发送 fence：不判重、不认领、不消费、
    不设 TTL。「这句话要不要再说一次」仍然只由下一拍模型对着时间线判断
    （上下游边界契约 §4 不变）。
    """
    lock = _send_locks.get(scope_key)
    if lock is None:
        lock = asyncio.Lock()
        _send_locks[scope_key] = lock
    return lock


async def send_all(bot: Any, scope_key: str, prepared: list[dict]) -> list[dict]:
    """OneBot 逐条发送并生成回执；单条失败/存疑不阻断后续气泡。

    回执三态：``sent``（拿到 message_id）/ ``failed``（napcat 明确拒绝）/
    ``uncertain``（传输中断或 ok 无 message_id——可能已发出）。

    整段持 ``send_lock(scope_key)``：同 scope 的并行程序按到达顺序逐次发送，
    气泡不交错。
    """
    async with send_lock(scope_key):
        return await _send_all_locked(bot, scope_key, prepared)


async def _send_all_locked(
    bot: Any, scope_key: str, prepared: list[dict]
) -> list[dict]:
    from qqbot.services.agent_loop.event_writer import parse_scope_key
    from qqbot.services.agent_loop.tools._onebot_common import call_action

    _, group_id, _ = parse_scope_key(scope_key)
    receipts: list[dict] = []
    for index, item in enumerate(prepared):
        if item["kind"] == "chat":
            content = build_chat_content(item)
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
            response, action_fail = await call_action(
                bot,
                "send_group_msg",
                effect=True,
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
                _receipt_from_action_failure(index, public_item, action_fail)
            )
            continue
        if response is None:
            receipts.append(
                uncertain_receipt(
                    index,
                    public_item,
                    "missing_gateway_response",
                    "OneBot gateway returned neither response nor failure",
                )
            )
            continue
        message_id = extract_message_id(response.data)
        if message_id is None:
            receipts.append(
                uncertain_receipt(
                    index,
                    public_item,
                    "missing_message_id",
                    "upstream returned ok but no message_id",
                    response,
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
                "receipt": public_receipt(response),
            }
        )
    return receipts


def _receipt_from_action_failure(
    index: int, item: dict, failure: ToolOutcome
) -> dict:
    if failure.extra.get("status") != "uncertain":
        return failed_receipt(
            index,
            item,
            failure.error_kind,
            failure.error_message,
            failure.extra,
        )
    error_kind = (
        failure.extra.get("transport_error_kind")
        or failure.error_kind
        or "upstream_delivery_uncertain"
    )
    return uncertain_receipt(
        index,
        item,
        str(error_kind),
        failure.error_message or "OneBot response is unknown",
        failure.extra,
    )


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
    if any(status == "uncertain" for status in statuses):
        return "uncertain"
    if statuses and all(status == "sent" for status in statuses):
        return "sent"
    if any(status == "sent" for status in statuses):
        return "partial"
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
    if isinstance(value, RawOneBotResponse):
        value = value.as_dict()
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

"""Small duck-typed helpers for working with nonebot OneBot V11 events.

Mappers stay decoupled from concrete nonebot types by going through these
helpers; tests can pass plain SimpleNamespace fakes.
"""

from __future__ import annotations

from typing import Any


def dump_event(event: Any) -> dict:
    """Best-effort serialize a nonebot Event to a plain dict.

    Tries pydantic v2 (`model_dump`) first, then pydantic v1 (`dict`),
    then returns {} so ingest never crashes on a malformed event.
    """
    for attr in ("model_dump", "dict"):
        fn = getattr(event, attr, None)
        if callable(fn):
            try:
                return dict(fn())
            except Exception:
                pass
    return {}


def dump_segments(message: Any) -> list[dict]:
    """Serialize a nonebot Message (iterable of MessageSegment) to plain dicts."""
    if message is None:
        return []
    out: list[dict] = []
    for seg in message:
        if isinstance(seg, dict):
            out.append(seg)
            continue
        try:
            seg_type = getattr(seg, "type", None)
            seg_data = dict(getattr(seg, "data", {}) or {})
            out.append({"type": seg_type, "data": seg_data})
        except Exception:
            out.append({"type": "unknown", "data": {}})
    return out


def dump_message_segments(event: Any) -> list[dict]:
    """消息事件 → 段数组，取适配器改写前的 ``original_message``。

    nonebot 的 OneBot V11 适配器在分发给 matcher 之前会原地改写
    ``event.message``：``_check_reply`` 把 reply 段解析进 ``event.reply``
    后**删除该段**（若紧随其后是 @bot 段——客户端引用默认附带——一并
    删除，并 lstrip 后面的文本），``_check_at_me`` 再剥掉首/尾的 @bot 段。
    直接 dump ``event.message`` 会让"引用 + @bot"类消息在事件流里退化成
    裸文本。``event.original_message`` 是改写前的深拷贝，才是 napcat
    真实上报的完整消息；缺失或为空时（测试 fake、非 v11 适配器）回退
    ``event.message``。
    """
    original = getattr(event, "original_message", None)
    if original:
        return dump_segments(original)
    return dump_segments(getattr(event, "message", None))

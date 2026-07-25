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


def enrich_reply_segments(event: Any, segments: list[dict]) -> None:
    """把适配器已解析的被引消息（``event.reply``）固化进首个 reply 段。

    nonebot OneBot V11 适配器收到带 reply 段的消息时，分发前已经调过
    ``get_msg`` 把被引消息解析成 ``event.reply``（Reply 模型：sender +
    message + message_id）——被引消息的作者与原文在 ingest 时刻**现成可得，
    零额外 API 调用**。这里把它写进事件 payload，投影渲染 reply 段时优先
    消费；被引消息滚出投影窗口后 from_*/excerpt 不再丢失（2026-07-22
    出窗引用黑洞修复）。

    落键（segment 顶层 ``quoted``，与 media 富化同级；子键"有才落键"）：
    - ``sender_qq`` / ``sender_name``：被引消息作者的 QQ 号与显示名
      （card 优先、nickname 兜底）。
    - ``from_self``：被引消息是否 bot 自己所发（sender_qq == self_id，
      ingest 时刻的服务端事实；bool，两值都落，渲染层只在 true 时输出）。
    - ``segments``：被引消息的段数组 dump（供投影生成 excerpt；**不做**
      媒体下载——media 富化只走 payload 顶层 segments）。

    ``event.reply`` 缺失（适配器 get_msg 失败、被引消息已撤回、非 v11
    适配器、测试 fake）时不落键，投影退回窗口内索引兜底。OneBot V11 一条
    消息至多一个 reply 段，固定富化第一个、不按 id 匹配——napcat 的
    get_msg 返回 id 与段内 id 可能分属 message_id / real_id 两个空间，
    严格匹配反而把现成的富化白白丢掉。
    """
    reply = getattr(event, "reply", None)
    if reply is None:
        return
    target = next(
        (
            seg
            for seg in segments
            if isinstance(seg, dict) and seg.get("type") == "reply"
        ),
        None,
    )
    if target is None:
        return
    quoted: dict[str, Any] = {}
    sender = getattr(reply, "sender", None)
    sender_qq = getattr(sender, "user_id", None) if sender is not None else None
    if sender_qq is not None and str(sender_qq).strip():
        quoted["sender_qq"] = str(sender_qq)
    name = None
    if sender is not None:
        name = getattr(sender, "card", None) or getattr(sender, "nickname", None)
    if name is not None and str(name).strip():
        quoted["sender_name"] = str(name)
    self_id = getattr(event, "self_id", None)
    if sender_qq is not None and self_id is not None and str(self_id).strip():
        quoted["from_self"] = str(sender_qq) == str(self_id)
    quoted["segments"] = dump_segments(getattr(reply, "message", None))
    target["quoted"] = quoted

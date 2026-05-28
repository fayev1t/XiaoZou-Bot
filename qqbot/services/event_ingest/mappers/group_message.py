"""Map OneBot V11 GroupMessageEvent → external.message.group.*

Contract: 开发文档/v2.0/事件系统设计.md §4.1
"""

from __future__ import annotations

from typing import Any

from qqbot.core.ids import new_msg_hash
from qqbot.services.event_ingest import idempotency
from qqbot.services.event_ingest.napcat_helpers import dump_event, dump_segments
from qqbot.services.event_ingest.system_event import PartialSystemEvent

_SUBTYPE_TO_TYPE = {
    "normal": "external.message.group.normal",
    "anonymous": "external.message.group.anonymous",
    "notice": "external.message.group.notice",
}


class GroupMessageMapper:
    post_type = "message"
    # MapperRegistry 把 sub_type=None 当 fallback 处理；即使 OneBot V11 在
    # message 上的判别器实际是 message_type 而不是 sub_type，这里也要给一个
    # 非 None 的 classifier，让本 mapper 在 find() 里走"exact"分支，盖过
    # 真正的 catch-all fallback。取值仅供 registry 分流，不影响 payload。
    sub_type = "group_message"

    def can_map(self, event: Any) -> bool:
        return (
            getattr(event, "post_type", None) == "message"
            and getattr(event, "message_type", None) == "group"
        )

    def map(self, event: Any) -> PartialSystemEvent:
        msg_sub_type = getattr(event, "sub_type", "normal") or "normal"
        type_name = _SUBTYPE_TO_TYPE.get(msg_sub_type, "external.message.group.normal")

        payload = {
            "msg_hash": new_msg_hash(),
            "onebot_message_id": str(getattr(event, "message_id", "")),
            "raw_message": getattr(event, "raw_message", "") or "",
            "sender": _dump_sender(event),
            "segments": dump_segments(getattr(event, "message", None)),
            "message_sub_type": msg_sub_type,
        }

        return PartialSystemEvent(
            origin="external",
            type=type_name,
            scope="group",
            group_id=getattr(event, "group_id", None),
            user_id=getattr(event, "user_id", None),
            visibility="agent_visible",
            payload=payload,
            raw=dump_event(event),
            idempotency_key=idempotency.for_message(
                getattr(event, "self_id", 0),
                getattr(event, "message_id", ""),
            ),
        )


def _dump_sender(event: Any) -> dict:
    sender = getattr(event, "sender", None)
    if sender is None:
        return {"user_id": getattr(event, "user_id", None)}
    return {
        "user_id": getattr(sender, "user_id", None),
        "nickname": getattr(sender, "nickname", None),
        "card": getattr(sender, "card", None),
        "role": getattr(sender, "role", None),
    }

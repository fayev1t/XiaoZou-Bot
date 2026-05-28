"""Map OneBot V11 PrivateMessageEvent → external.message.private

scope=private。v2 第一版不实例化 PrivateAgentLoop（实例化策略 §10.1），
但事件依然入库以便审计与未来扩展。
"""

from __future__ import annotations

from typing import Any

from qqbot.core.ids import new_msg_hash
from qqbot.services.event_ingest import idempotency
from qqbot.services.event_ingest.napcat_helpers import dump_event, dump_segments
from qqbot.services.event_ingest.system_event import PartialSystemEvent


class PrivateMessageMapper:
    post_type = "message"
    # 同 GroupMessageMapper 注释：MapperRegistry 用 sub_type=None 表 fallback，
    # 这里给非 None classifier 让 find() 当 exact 处理。判别仍由 can_map 看
    # message_type=private 完成。
    sub_type = "private_message"

    def can_map(self, event: Any) -> bool:
        return (
            getattr(event, "post_type", None) == "message"
            and getattr(event, "message_type", None) == "private"
        )

    def map(self, event: Any) -> PartialSystemEvent:
        sender = getattr(event, "sender", None)
        payload = {
            "msg_hash": new_msg_hash(),
            "onebot_message_id": str(getattr(event, "message_id", "")),
            "raw_message": getattr(event, "raw_message", "") or "",
            "sender": {
                "user_id": getattr(sender, "user_id", None) if sender else None,
                "nickname": getattr(sender, "nickname", None) if sender else None,
            },
            "segments": dump_segments(getattr(event, "message", None)),
            "message_sub_type": getattr(event, "sub_type", "friend") or "friend",
        }
        return PartialSystemEvent(
            origin="external",
            type="external.message.private",
            scope="private",
            group_id=None,
            user_id=getattr(event, "user_id", None),
            visibility="agent_visible",
            payload=payload,
            raw=dump_event(event),
            idempotency_key=idempotency.for_message(
                getattr(event, "self_id", 0),
                getattr(event, "message_id", ""),
            ),
        )

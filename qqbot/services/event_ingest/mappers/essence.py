"""Map napcat notice.essence → external.notice.essence (精华消息)"""

from __future__ import annotations

from typing import Any

from qqbot.services.event_ingest.napcat_helpers import dump_event
from qqbot.services.event_ingest.system_event import PartialSystemEvent


class EssenceMapper:
    post_type = "notice"
    sub_type = "essence"

    def can_map(self, event: Any) -> bool:
        return (
            getattr(event, "post_type", None) == "notice"
            and getattr(event, "notice_type", None) == "essence"
        )

    def map(self, event: Any) -> PartialSystemEvent:
        time_value = int(getattr(event, "time", 0) or 0)
        message_id = getattr(event, "message_id", "")
        sub = getattr(event, "sub_type", None)  # add / delete
        self_id = getattr(event, "self_id", 0)
        payload = {
            "sub_type": sub,
            "onebot_message_id": str(message_id),
            "sender_id": getattr(event, "sender_id", None),
            "operator_id": getattr(event, "operator_id", None),
        }
        return PartialSystemEvent(
            origin="external",
            type="external.notice.essence",
            scope="group",
            group_id=getattr(event, "group_id", None),
            user_id=getattr(event, "sender_id", None),
            visibility="agent_visible",
            payload=payload,
            raw=dump_event(event),
            idempotency_key=f"{self_id}:essence:{message_id}:{sub or '_'}:{time_value}",
        )

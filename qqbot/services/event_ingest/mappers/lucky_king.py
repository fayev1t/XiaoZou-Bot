"""Map notice.notify.lucky_king → external.notice.lucky_king"""

from __future__ import annotations

from typing import Any

from qqbot.services.event_ingest import idempotency
from qqbot.services.event_ingest.napcat_helpers import dump_event
from qqbot.services.event_ingest.system_event import PartialSystemEvent


class LuckyKingMapper:
    post_type = "notice"
    sub_type = "lucky_king"

    def can_map(self, event: Any) -> bool:
        if getattr(event, "post_type", None) != "notice":
            return False
        return (
            getattr(event, "notice_type", None) == "notify"
            and getattr(event, "sub_type", None) == "lucky_king"
        )

    def map(self, event: Any) -> PartialSystemEvent:
        time_value = int(getattr(event, "time", 0) or 0)
        target_id = getattr(event, "target_id", None)
        payload = {
            "sender_id": getattr(event, "user_id", None),
            "target_id": target_id,
        }
        return PartialSystemEvent(
            origin="external",
            type="external.notice.lucky_king",
            scope="group",
            group_id=getattr(event, "group_id", None),
            user_id=getattr(event, "user_id", None),
            visibility="agent_visible",
            payload=payload,
            raw=dump_event(event),
            idempotency_key=idempotency.for_notice(
                getattr(event, "self_id", 0),
                "notify",
                "lucky_king",
                time_value,
                getattr(event, "user_id", None),
                target_id,
            ),
        )

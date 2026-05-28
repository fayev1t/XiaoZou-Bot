"""Map notice.friend_recall → external.notice.friend_recall"""

from __future__ import annotations

from typing import Any

from qqbot.services.event_ingest import idempotency
from qqbot.services.event_ingest.napcat_helpers import dump_event
from qqbot.services.event_ingest.system_event import PartialSystemEvent


class FriendRecallMapper:
    post_type = "notice"
    sub_type = "friend_recall"

    def can_map(self, event: Any) -> bool:
        return (
            getattr(event, "post_type", None) == "notice"
            and getattr(event, "notice_type", None) == "friend_recall"
        )

    def map(self, event: Any) -> PartialSystemEvent:
        time_value = int(getattr(event, "time", 0) or 0)
        message_id = getattr(event, "message_id", "")
        payload = {
            "onebot_message_id": str(message_id),
        }
        return PartialSystemEvent(
            origin="external",
            type="external.notice.friend_recall",
            scope="private",
            group_id=None,
            user_id=getattr(event, "user_id", None),
            visibility="agent_visible",
            payload=payload,
            raw=dump_event(event),
            idempotency_key=idempotency.for_recall(
                getattr(event, "self_id", 0),
                message_id,
                time_value,
            ),
        )

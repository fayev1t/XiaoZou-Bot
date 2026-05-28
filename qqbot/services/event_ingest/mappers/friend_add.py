"""Map notice.friend_add → external.notice.friend_add"""

from __future__ import annotations

from typing import Any

from qqbot.services.event_ingest import idempotency
from qqbot.services.event_ingest.napcat_helpers import dump_event
from qqbot.services.event_ingest.system_event import PartialSystemEvent


class FriendAddMapper:
    post_type = "notice"
    sub_type = "friend_add"

    def can_map(self, event: Any) -> bool:
        return (
            getattr(event, "post_type", None) == "notice"
            and getattr(event, "notice_type", None) == "friend_add"
        )

    def map(self, event: Any) -> PartialSystemEvent:
        time_value = int(getattr(event, "time", 0) or 0)
        return PartialSystemEvent(
            origin="external",
            type="external.notice.friend_add",
            scope="private",
            group_id=None,
            user_id=getattr(event, "user_id", None),
            visibility="agent_visible",
            payload={},
            raw=dump_event(event),
            idempotency_key=idempotency.for_notice(
                getattr(event, "self_id", 0),
                "friend_add",
                None,
                time_value,
                getattr(event, "user_id", None),
                None,
            ),
        )

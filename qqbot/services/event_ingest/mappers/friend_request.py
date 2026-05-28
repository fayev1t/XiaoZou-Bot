"""Map request.friend → external.request.friend (scope=system)

加好友申请；由 SystemAgentLoop 决策是否同意。
"""

from __future__ import annotations

from typing import Any

from qqbot.services.event_ingest import idempotency
from qqbot.services.event_ingest.napcat_helpers import dump_event
from qqbot.services.event_ingest.system_event import PartialSystemEvent


class FriendRequestMapper:
    post_type = "request"
    sub_type = "friend"

    def can_map(self, event: Any) -> bool:
        return (
            getattr(event, "post_type", None) == "request"
            and getattr(event, "request_type", None) == "friend"
        )

    def map(self, event: Any) -> PartialSystemEvent:
        flag = str(getattr(event, "flag", ""))
        payload = {
            "user_id": getattr(event, "user_id", None),
            "comment": getattr(event, "comment", None),
            "flag": flag,
        }
        return PartialSystemEvent(
            origin="external",
            type="external.request.friend",
            scope="system",
            group_id=None,
            user_id=getattr(event, "user_id", None),
            visibility="agent_visible",
            payload=payload,
            raw=dump_event(event),
            idempotency_key=idempotency.for_request(
                getattr(event, "self_id", 0),
                "friend",
                flag,
            ),
        )

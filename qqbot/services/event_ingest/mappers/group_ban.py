"""Map notice.group_ban → external.notice.group_ban"""

from __future__ import annotations

from typing import Any

from qqbot.services.event_ingest import idempotency
from qqbot.services.event_ingest.napcat_helpers import dump_event
from qqbot.services.event_ingest.system_event import PartialSystemEvent


class GroupBanMapper:
    post_type = "notice"
    sub_type = "group_ban"

    def can_map(self, event: Any) -> bool:
        return (
            getattr(event, "post_type", None) == "notice"
            and getattr(event, "notice_type", None) == "group_ban"
        )

    def map(self, event: Any) -> PartialSystemEvent:
        time_value = int(getattr(event, "time", 0) or 0)
        payload = {
            "sub_type": getattr(event, "sub_type", None),  # ban / lift_ban
            "operator_id": getattr(event, "operator_id", None),
            "duration": getattr(event, "duration", None),
        }
        return PartialSystemEvent(
            origin="external",
            type="external.notice.group_ban",
            scope="group",
            group_id=getattr(event, "group_id", None),
            user_id=getattr(event, "user_id", None),
            visibility="agent_visible",
            payload=payload,
            raw=dump_event(event),
            idempotency_key=idempotency.for_notice(
                getattr(event, "self_id", 0),
                "group_ban",
                payload["sub_type"],
                time_value,
                getattr(event, "user_id", None),
                payload.get("operator_id"),
            ),
        )

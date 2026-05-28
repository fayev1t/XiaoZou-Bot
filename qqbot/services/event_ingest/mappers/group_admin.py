"""Map notice.group_admin → external.notice.group_admin"""

from __future__ import annotations

from typing import Any

from qqbot.services.event_ingest import idempotency
from qqbot.services.event_ingest.napcat_helpers import dump_event
from qqbot.services.event_ingest.system_event import PartialSystemEvent


class GroupAdminMapper:
    post_type = "notice"
    sub_type = "group_admin"

    def can_map(self, event: Any) -> bool:
        return (
            getattr(event, "post_type", None) == "notice"
            and getattr(event, "notice_type", None) == "group_admin"
        )

    def map(self, event: Any) -> PartialSystemEvent:
        time_value = int(getattr(event, "time", 0) or 0)
        payload = {
            "sub_type": getattr(event, "sub_type", None),  # set / unset
        }
        return PartialSystemEvent(
            origin="external",
            type="external.notice.group_admin",
            scope="group",
            group_id=getattr(event, "group_id", None),
            user_id=getattr(event, "user_id", None),
            visibility="agent_visible",
            payload=payload,
            raw=dump_event(event),
            idempotency_key=idempotency.for_notice(
                getattr(event, "self_id", 0),
                "group_admin",
                payload["sub_type"],
                time_value,
                getattr(event, "user_id", None),
                None,
            ),
        )

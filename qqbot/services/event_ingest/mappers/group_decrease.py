"""Map notice.group_decrease → external.notice.group_decrease"""

from __future__ import annotations

from typing import Any

from qqbot.services.event_ingest import idempotency
from qqbot.services.event_ingest.napcat_helpers import dump_event
from qqbot.services.event_ingest.system_event import PartialSystemEvent


class GroupDecreaseMapper:
    post_type = "notice"
    sub_type = "group_decrease"

    def can_map(self, event: Any) -> bool:
        return (
            getattr(event, "post_type", None) == "notice"
            and getattr(event, "notice_type", None) == "group_decrease"
        )

    def map(self, event: Any) -> PartialSystemEvent:
        time_value = int(getattr(event, "time", 0) or 0)
        payload = {
            "sub_type": getattr(event, "sub_type", None),  # leave / kick / kick_me
            "operator_id": getattr(event, "operator_id", None),
        }
        return PartialSystemEvent(
            origin="external",
            type="external.notice.group_decrease",
            scope="group",
            group_id=getattr(event, "group_id", None),
            user_id=getattr(event, "user_id", None),
            visibility="agent_visible",
            payload=payload,
            raw=dump_event(event),
            idempotency_key=idempotency.for_notice(
                getattr(event, "self_id", 0),
                "group_decrease",
                payload["sub_type"],
                time_value,
                getattr(event, "user_id", None),
                payload.get("operator_id"),
            ),
        )

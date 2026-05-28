"""Map notice.group_increase → external.notice.group_increase"""

from __future__ import annotations

from typing import Any

from qqbot.services.event_ingest import idempotency
from qqbot.services.event_ingest.napcat_helpers import dump_event
from qqbot.services.event_ingest.system_event import PartialSystemEvent


class GroupIncreaseMapper:
    post_type = "notice"
    sub_type = "group_increase"

    def can_map(self, event: Any) -> bool:
        return (
            getattr(event, "post_type", None) == "notice"
            and getattr(event, "notice_type", None) == "group_increase"
        )

    def map(self, event: Any) -> PartialSystemEvent:
        time_value = int(getattr(event, "time", 0) or 0)
        payload = {
            "sub_type": getattr(event, "sub_type", None),  # approve / invite
            "operator_id": getattr(event, "operator_id", None),
        }
        return PartialSystemEvent(
            origin="external",
            type="external.notice.group_increase",
            scope="group",
            group_id=getattr(event, "group_id", None),
            user_id=getattr(event, "user_id", None),
            visibility="agent_visible",
            payload=payload,
            raw=dump_event(event),
            idempotency_key=idempotency.for_notice(
                getattr(event, "self_id", 0),
                "group_increase",
                payload["sub_type"],
                time_value,
                payload.get("operator_id"),
                getattr(event, "user_id", None),
            ),
        )

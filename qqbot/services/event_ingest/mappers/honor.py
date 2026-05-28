"""Map notice.notify.honor → external.notice.honor"""

from __future__ import annotations

from typing import Any

from qqbot.services.event_ingest import idempotency
from qqbot.services.event_ingest.napcat_helpers import dump_event
from qqbot.services.event_ingest.system_event import PartialSystemEvent


class HonorMapper:
    post_type = "notice"
    sub_type = "honor"

    def can_map(self, event: Any) -> bool:
        if getattr(event, "post_type", None) != "notice":
            return False
        return (
            getattr(event, "notice_type", None) == "notify"
            and getattr(event, "sub_type", None) == "honor"
        )

    def map(self, event: Any) -> PartialSystemEvent:
        time_value = int(getattr(event, "time", 0) or 0)
        payload = {
            "honor_type": getattr(event, "honor_type", None),
        }
        return PartialSystemEvent(
            origin="external",
            type="external.notice.honor",
            scope="group",
            group_id=getattr(event, "group_id", None),
            user_id=getattr(event, "user_id", None),
            visibility="agent_visible",
            payload=payload,
            raw=dump_event(event),
            idempotency_key=idempotency.for_notice(
                getattr(event, "self_id", 0),
                "notify",
                "honor",
                time_value,
                getattr(event, "user_id", None),
                None,
            ),
        )

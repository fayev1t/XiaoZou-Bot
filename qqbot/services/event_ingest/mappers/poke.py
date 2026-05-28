"""Map notice.notify.poke → external.notice.poke

戳一戳在群里和私聊里都可能出现，按报文有无 group_id 决定 scope。
"""

from __future__ import annotations

from typing import Any

from qqbot.services.event_ingest import idempotency
from qqbot.services.event_ingest.napcat_helpers import dump_event
from qqbot.services.event_ingest.system_event import PartialSystemEvent


class PokeMapper:
    post_type = "notice"
    sub_type = "poke"

    def can_map(self, event: Any) -> bool:
        if getattr(event, "post_type", None) != "notice":
            return False
        # OneBot V11 notify family: notice_type=notify, sub_type=poke
        notice_type = getattr(event, "notice_type", None)
        sub = getattr(event, "sub_type", None)
        if notice_type == "notify" and sub == "poke":
            return True
        # napcat 实现里也有把 notice_type 直接置为 poke 的旁路；都接住。
        return notice_type == "poke"

    def map(self, event: Any) -> PartialSystemEvent:
        time_value = int(getattr(event, "time", 0) or 0)
        group_id = getattr(event, "group_id", None)
        scope = "group" if group_id else "private"
        target_id = getattr(event, "target_id", None)
        payload = {
            "sender_id": getattr(event, "user_id", None),
            "target_id": target_id,
        }
        return PartialSystemEvent(
            origin="external",
            type="external.notice.poke",
            scope=scope,
            group_id=group_id,
            user_id=getattr(event, "user_id", None),
            visibility="agent_visible",
            payload=payload,
            raw=dump_event(event),
            idempotency_key=idempotency.for_notice(
                getattr(event, "self_id", 0),
                "notify",
                "poke",
                time_value,
                getattr(event, "user_id", None),
                target_id,
            ),
        )

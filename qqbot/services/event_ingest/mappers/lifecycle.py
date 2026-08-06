"""Map meta_event.lifecycle → external.meta.lifecycle

scope=system，visibility=runtime_only。提交后 plugin 唤醒 system scope；
connect 的 bot 角色全量扫描只由刚提交的 SystemEvent 反射触发。
"""

from __future__ import annotations

from typing import Any

from qqbot.services.event_ingest import idempotency
from qqbot.services.event_ingest.napcat_helpers import dump_event
from qqbot.services.event_ingest.system_event import PartialSystemEvent


class LifecycleMapper:
    post_type = "meta_event"
    sub_type = "lifecycle"

    def can_map(self, event: Any) -> bool:
        return (
            getattr(event, "post_type", None) == "meta_event"
            and getattr(event, "meta_event_type", None) == "lifecycle"
        )

    def map(self, event: Any) -> PartialSystemEvent:
        time_value = int(getattr(event, "time", 0) or 0)
        sub = getattr(event, "sub_type", None) or "unknown"  # connect/enable/disable
        payload = {"sub_type": sub}
        return PartialSystemEvent(
            origin="external",
            type="external.meta.lifecycle",
            scope="system",
            group_id=None,
            user_id=None,
            visibility="runtime_only",
            payload=payload,
            raw=dump_event(event),
            idempotency_key=idempotency.for_lifecycle(
                getattr(event, "self_id", 0),
                sub,
                time_value,
            ),
        )

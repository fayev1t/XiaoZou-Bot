"""Map napcat notice.input_status → external.notice.input_status

visibility=runtime_only：对方正在输入状态，不进入 LLM 上下文投影，
仅作为运维侧信号（如检测对端连接活跃度）。
"""

from __future__ import annotations

from typing import Any

from qqbot.services.event_ingest import idempotency
from qqbot.services.event_ingest.napcat_helpers import dump_event
from qqbot.services.event_ingest.system_event import PartialSystemEvent


class InputStatusMapper:
    post_type = "notice"
    sub_type = "input_status"

    def can_map(self, event: Any) -> bool:
        return (
            getattr(event, "post_type", None) == "notice"
            and getattr(event, "notice_type", None) == "input_status"
        )

    def map(self, event: Any) -> PartialSystemEvent:
        time_value = int(getattr(event, "time", 0) or 0)
        sub = getattr(event, "sub_type", None)
        payload = {
            "status_sub_type": sub,
            "status_text": getattr(event, "status_text", None),
            "event_type": getattr(event, "event_type", None),
        }
        return PartialSystemEvent(
            origin="external",
            type="external.notice.input_status",
            scope="private",
            group_id=None,
            user_id=getattr(event, "user_id", None),
            visibility="runtime_only",
            payload=payload,
            raw=dump_event(event),
            idempotency_key=idempotency.for_notice(
                getattr(event, "self_id", 0),
                "input_status",
                sub,
                time_value,
                getattr(event, "user_id", None),
                None,
            ),
        )

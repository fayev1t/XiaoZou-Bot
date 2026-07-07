"""Map request.friend → external.request.friend (scope=system, runtime_only)

加好友申请不走 LLM 决策：runtime_only 纯审计落库（不进任何 timeline 投影），
由 plugin 层 request_auto_approval 在入库后自动同意并补审计事件。
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
            visibility="runtime_only",
            payload=payload,
            raw=dump_event(event),
            idempotency_key=idempotency.for_request(
                getattr(event, "self_id", 0),
                "friend",
                flag,
            ),
        )

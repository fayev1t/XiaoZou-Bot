"""Map request.group → external.request.group.{add,invite}

加群申请 / 邀请入群；scope=system，由 SystemAgentLoop 决策。
"""

from __future__ import annotations

from typing import Any

from qqbot.services.event_ingest import idempotency
from qqbot.services.event_ingest.napcat_helpers import dump_event
from qqbot.services.event_ingest.system_event import PartialSystemEvent

_SUBTYPE_TO_TYPE = {
    "add": "external.request.group.add",
    "invite": "external.request.group.invite",
}


class GroupRequestMapper:
    post_type = "request"
    sub_type = "group"

    def can_map(self, event: Any) -> bool:
        return (
            getattr(event, "post_type", None) == "request"
            and getattr(event, "request_type", None) == "group"
        )

    def map(self, event: Any) -> PartialSystemEvent:
        sub = getattr(event, "sub_type", "add") or "add"
        type_name = _SUBTYPE_TO_TYPE.get(sub, "external.request.group.add")
        flag = str(getattr(event, "flag", ""))
        payload = {
            "sub_type": sub,
            "group_id": getattr(event, "group_id", None),
            "user_id": getattr(event, "user_id", None),
            "comment": getattr(event, "comment", None),
            "flag": flag,
        }
        return PartialSystemEvent(
            origin="external",
            type=type_name,
            scope="system",
            group_id=None,  # scope=system 路由到 SystemAgentLoop；group_id 在 payload 留作上下文
            user_id=getattr(event, "user_id", None),
            visibility="agent_visible",
            payload=payload,
            raw=dump_event(event),
            idempotency_key=idempotency.for_request(
                getattr(event, "self_id", 0),
                "group",
                flag,
            ),
        )

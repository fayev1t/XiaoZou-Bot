"""Map napcat notice.bot_offline → external.notice.bot_offline

scope=system，visibility=runtime_only。SystemAgentLoop 通过此事件感知
适配器掉线，运维侧告警。
"""

from __future__ import annotations

from typing import Any

from qqbot.services.event_ingest import idempotency
from qqbot.services.event_ingest.napcat_helpers import dump_event
from qqbot.services.event_ingest.system_event import PartialSystemEvent


class BotOfflineMapper:
    post_type = "notice"
    sub_type = "bot_offline"

    def can_map(self, event: Any) -> bool:
        return (
            getattr(event, "post_type", None) == "notice"
            and getattr(event, "notice_type", None) == "bot_offline"
        )

    def map(self, event: Any) -> PartialSystemEvent:
        time_value = int(getattr(event, "time", 0) or 0)
        payload = {
            "tag": getattr(event, "tag", None),
            "message": getattr(event, "message", None),
        }
        return PartialSystemEvent(
            origin="external",
            type="external.notice.bot_offline",
            scope="system",
            group_id=None,
            user_id=None,
            visibility="runtime_only",
            payload=payload,
            raw=dump_event(event),
            idempotency_key=idempotency.for_notice(
                getattr(event, "self_id", 0),
                "bot_offline",
                None,
                time_value,
                None,
                None,
            ),
        )

"""Map napcat notice.group_msg_emoji_like → external.notice.emoji_like"""

from __future__ import annotations

from typing import Any

from qqbot.services.event_ingest.napcat_helpers import dump_event
from qqbot.services.event_ingest.system_event import PartialSystemEvent


class EmojiLikeMapper:
    post_type = "notice"
    sub_type = "group_msg_emoji_like"

    def can_map(self, event: Any) -> bool:
        return (
            getattr(event, "post_type", None) == "notice"
            and getattr(event, "notice_type", None) == "group_msg_emoji_like"
        )

    def map(self, event: Any) -> PartialSystemEvent:
        time_value = int(getattr(event, "time", 0) or 0)
        message_id = getattr(event, "message_id", "")
        likes_raw = getattr(event, "likes", None) or []
        likes: list[dict] = []
        for item in likes_raw:
            if isinstance(item, dict):
                likes.append(item)
            else:
                likes.append({
                    "emoji_id": getattr(item, "emoji_id", None),
                    "count": getattr(item, "count", None),
                })
        self_id = getattr(event, "self_id", 0)
        payload = {
            "onebot_message_id": str(message_id),
            "likes": likes,
        }
        return PartialSystemEvent(
            origin="external",
            type="external.notice.emoji_like",
            scope="group",
            group_id=getattr(event, "group_id", None),
            user_id=getattr(event, "user_id", None),
            visibility="agent_visible",
            payload=payload,
            raw=dump_event(event),
            idempotency_key=f"{self_id}:emoji_like:{message_id}:{time_value}",
        )

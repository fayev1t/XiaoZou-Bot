"""Map OneBot V11 GroupRecallNoticeEvent → external.notice.group_recall

Contract: 开发文档/v2.0/事件系统设计.md §4.1
撤回作为追加事件，不物理改写被撤回的消息事件（事件系统设计.md §5.1）。
"""

from __future__ import annotations

from typing import Any

from qqbot.services.event_ingest import idempotency
from qqbot.services.event_ingest.napcat_helpers import dump_event
from qqbot.services.event_ingest.system_event import PartialSystemEvent


class GroupRecallMapper:
    post_type = "notice"
    sub_type = "group_recall"

    def can_map(self, event: Any) -> bool:
        return (
            getattr(event, "post_type", None) == "notice"
            and getattr(event, "notice_type", None) == "group_recall"
        )

    def map(self, event: Any) -> PartialSystemEvent:
        time_value = int(getattr(event, "time", 0) or 0)
        message_id = getattr(event, "message_id", "")

        payload = {
            "onebot_message_id": str(message_id),
            "operator_id": getattr(event, "operator_id", None),
            # recalled_event_id 由消费投影时按 onebot_message_id 反查事件流补全；
            # ingest 阶段没有跨表读取权限，避免与事件流的 append-only 约束冲突。
        }

        return PartialSystemEvent(
            origin="external",
            type="external.notice.group_recall",
            scope="group",
            group_id=getattr(event, "group_id", None),
            user_id=getattr(event, "user_id", None),
            visibility="agent_visible",
            payload=payload,
            raw=dump_event(event),
            idempotency_key=idempotency.for_recall(
                getattr(event, "self_id", 0),
                message_id,
                time_value,
            ),
        )

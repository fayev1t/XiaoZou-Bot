"""Map request.group → external.request.group.{add,invite}

处理策略按 sub_type 分叉（拆分自原"统一交 SystemAgentLoop 决策"链路）：

- **add（入群申请）**：scope=group + group_id=目标群 → 进该群 timeline、像普通
  群事件一样唤醒 GroupAgentLoop。群内小奏看到 ``<request kind="group.add"/>``
  后可提醒，实际批准/拒绝须管理员明确授权并经 respond_to_group_join_request
  工具回执。visibility=agent_visible。
- **invite（邀请 bot 入群）**：不走 LLM。scope=system + runtime_only 纯审计
  落库，由 plugin 层 request_auto_approval 在入库后自动同意。
- **未知 sub_type**：兜底当 add 处理——宁可进群 timeline 要人授权，也不掉进
  自动同意通道。
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
        group_id = getattr(event, "group_id", None)
        payload = {
            "sub_type": sub,
            "group_id": group_id,
            "user_id": getattr(event, "user_id", None),
            "comment": getattr(event, "comment", None),
            "flag": flag,
        }
        # invite 走自动审批（runtime_only 审计）；其余（add + 未知兜底）进目标群
        # 的 timeline。add 但拿不到 group_id（理论不可能）退回 system 审计，避免
        # 造出 scope=group 而 group_id 为空、路由不到任何 loop 的悬空事件。
        if type_name == "external.request.group.invite" or group_id is None:
            scope, scope_group_id, visibility = "system", None, "runtime_only"
        else:
            scope, scope_group_id, visibility = "group", group_id, "agent_visible"
        return PartialSystemEvent(
            origin="external",
            type=type_name,
            scope=scope,
            group_id=scope_group_id,
            user_id=getattr(event, "user_id", None),
            visibility=visibility,
            payload=payload,
            raw=dump_event(event),
            idempotency_key=idempotency.for_request(
                getattr(event, "self_id", 0),
                "group",
                flag,
            ),
        )

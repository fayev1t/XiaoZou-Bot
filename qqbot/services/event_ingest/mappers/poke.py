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
        # napcat 扩展 raw_info：动作文案（"戳了戳"/自定义如"拍了拍"）与后缀
        # （如"的头"）。有值才落键；全量 raw_info 已随 raw 入库，payload 只
        # 留提炼值供投影渲染 action=/action_suffix=。
        action, suffix = _extract_action(getattr(event, "raw_info", None))
        if action:
            payload["action"] = action
        if suffix:
            payload["action_suffix"] = suffix
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


def _extract_action(raw_info: Any) -> tuple[str | None, str | None]:
    """napcat raw_info 段数组 → (动作文案, 后缀)。

    type=="nor" 段的 txt 依次是动作动词与可选后缀（qq/img 段是头像与跳链，
    无文本语义）。非 napcat 实现无 raw_info 字段 → (None, None)。
    """
    if not isinstance(raw_info, (list, tuple)):
        return None, None
    texts: list[str] = []
    for entry in raw_info:
        if isinstance(entry, dict):
            etype = entry.get("type")
            txt = entry.get("txt")
        else:
            etype = getattr(entry, "type", None)
            txt = getattr(entry, "txt", None)
        if etype == "nor" and txt is not None and str(txt).strip():
            texts.append(str(txt).strip())
    action = texts[0] if texts else None
    suffix = texts[1] if len(texts) > 1 else None
    return action, suffix

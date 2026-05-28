"""Map notice.group_upload → external.notice.group_upload

群文件上传。媒体落盘策略见 EventIngest契约.md §6.3：v2 第一版仅保留 napcat
元数据 (file.id/name/size/url)，不下载。
"""

from __future__ import annotations

from typing import Any

from qqbot.services.event_ingest import idempotency
from qqbot.services.event_ingest.napcat_helpers import dump_event
from qqbot.services.event_ingest.system_event import PartialSystemEvent


class GroupUploadMapper:
    post_type = "notice"
    sub_type = "group_upload"

    def can_map(self, event: Any) -> bool:
        return (
            getattr(event, "post_type", None) == "notice"
            and getattr(event, "notice_type", None) == "group_upload"
        )

    def map(self, event: Any) -> PartialSystemEvent:
        time_value = int(getattr(event, "time", 0) or 0)
        file_obj = getattr(event, "file", None)
        file_payload: dict | None
        if file_obj is None:
            file_payload = None
        elif isinstance(file_obj, dict):
            file_payload = dict(file_obj)
        else:
            file_payload = {
                "id": getattr(file_obj, "id", None),
                "name": getattr(file_obj, "name", None),
                "size": getattr(file_obj, "size", None),
                "url": getattr(file_obj, "url", None),
                "busid": getattr(file_obj, "busid", None),
            }
        payload = {"file": file_payload}
        file_id = (file_payload or {}).get("id")
        return PartialSystemEvent(
            origin="external",
            type="external.notice.group_upload",
            scope="group",
            group_id=getattr(event, "group_id", None),
            user_id=getattr(event, "user_id", None),
            visibility="agent_visible",
            payload=payload,
            raw=dump_event(event),
            idempotency_key=idempotency.for_notice(
                getattr(event, "self_id", 0),
                "group_upload",
                str(file_id) if file_id is not None else None,
                time_value,
                getattr(event, "user_id", None),
                None,
            ),
        )

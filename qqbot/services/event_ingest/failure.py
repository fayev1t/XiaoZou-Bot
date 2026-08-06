"""Build the terminal internal event for failed NapCat preprocessing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from qqbot.services.event_ingest.idempotency import for_ingest_failure
from qqbot.services.event_ingest.napcat_helpers import (
    dump_event,
    dump_message_segments,
)
from qqbot.services.event_ingest.system_event import PartialSystemEvent, Scope

INGEST_FAILURE_EVENT_TYPE = "runtime.event_ingest_failed"


@dataclass(frozen=True, slots=True)
class IngestFailureDetail:
    """One safe, stable explanation of why preprocessing did not succeed."""

    stage: str
    error_code: str
    reason: str
    segment_index: int | None = None
    segment_type: str | None = None
    file_hash: str | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "stage": self.stage,
            "error_code": self.error_code,
            "reason": self.reason,
        }
        if self.segment_index is not None:
            payload["segment_index"] = self.segment_index
        if self.segment_type is not None:
            payload["segment_type"] = self.segment_type
        if self.file_hash is not None:
            payload["file_hash"] = self.file_hash
        return payload


def build_ingest_failure_event(
    event: Any,
    failures: tuple[IngestFailureDetail, ...],
    *,
    partial: PartialSystemEvent | None = None,
) -> PartialSystemEvent:
    """Convert a failed preprocessing attempt into one internal terminal event."""
    if not failures:
        raise ValueError("ingest failure event requires at least one failure")

    scope, group_id, user_id = _resolve_scope(event, partial)
    payload: dict[str, Any] = {
        "source_event_type": _source_event_type(event, partial),
        "source_post_type": getattr(event, "post_type", None),
        "failures": [failure.to_payload() for failure in failures],
    }
    source_sub_type = getattr(event, "sub_type", None)
    if source_sub_type is not None:
        payload["source_sub_type"] = str(source_sub_type)

    payload.update(_safe_source_context(event, partial))

    return PartialSystemEvent(
        origin="runtime",
        type=INGEST_FAILURE_EVENT_TYPE,
        scope=scope,
        group_id=group_id,
        user_id=user_id,
        visibility="agent_visible",
        payload=payload,
        raw=dump_event(event),
        idempotency_key=(
            partial.idempotency_key
            if partial is not None and partial.idempotency_key
            else for_ingest_failure(event)
        ),
    )


def _resolve_scope(
    event: Any,
    partial: PartialSystemEvent | None,
) -> tuple[Scope, int | None, int | None]:
    if partial is not None:
        return partial.scope, partial.group_id, partial.user_id

    group_id = _optional_int(getattr(event, "group_id", None))
    user_id = _optional_int(getattr(event, "user_id", None))
    if group_id is not None:
        return "group", group_id, user_id
    if (
        getattr(event, "post_type", None) == "message"
        and getattr(event, "message_type", None) == "private"
    ):
        return "private", None, user_id
    return "system", None, user_id


def _source_event_type(
    event: Any,
    partial: PartialSystemEvent | None,
) -> str:
    if partial is not None:
        return partial.type

    post_type = str(getattr(event, "post_type", None) or "unknown")
    detail = None
    for attr in (
        "message_type",
        "notice_type",
        "request_type",
        "meta_event_type",
    ):
        value = getattr(event, attr, None)
        if value:
            detail = str(value)
            break
    sub_type = getattr(event, "sub_type", None)
    parts = [post_type]
    if detail:
        parts.append(detail)
    if sub_type:
        parts.append(str(sub_type))
    return ".".join(parts)


def _safe_source_context(
    event: Any,
    partial: PartialSystemEvent | None,
) -> dict[str, Any]:
    source = partial.payload if partial is not None else {}
    payload: dict[str, Any] = {}

    message_id = source.get("onebot_message_id")
    if message_id is None:
        message_id = getattr(event, "message_id", None)
    if message_id is not None and str(message_id).strip():
        payload["source_message_id"] = str(message_id)

    raw_message = source.get("raw_message")
    if raw_message is None:
        raw_message = getattr(event, "raw_message", None)
    if isinstance(raw_message, str) and raw_message:
        payload["raw_message"] = raw_message

    sender = source.get("sender")
    if not isinstance(sender, dict):
        sender = _dump_sender(getattr(event, "sender", None))
    safe_sender = _safe_sender(sender)
    if safe_sender:
        payload["sender"] = safe_sender

    segments = source.get("segments")
    if not isinstance(segments, list):
        try:
            segments = dump_message_segments(event)
        except Exception:
            segments = []
    safe_segments = _safe_segments(segments)
    if safe_segments:
        payload["segments"] = safe_segments
    return payload


def _dump_sender(sender: Any) -> dict[str, Any]:
    if sender is None:
        return {}
    return {
        key: getattr(sender, key, None)
        for key in ("user_id", "nickname", "card", "role", "title")
    }


def _safe_sender(sender: dict[str, Any]) -> dict[str, Any]:
    return {
        key: sender[key]
        for key in ("user_id", "nickname", "card", "role", "title")
        if sender.get(key) is not None
    }


def _safe_segments(segments: list[Any]) -> list[dict[str, Any]]:
    safe: list[dict[str, Any]] = []
    allowed_data_fields = {
        "text": ("text",),
        "at": ("qq",),
        "reply": ("id",),
        "face": ("id",),
    }
    for segment in segments:
        if not isinstance(segment, dict):
            safe.append({"type": "unknown"})
            continue
        segment_type = str(segment.get("type") or "unknown")
        item: dict[str, Any] = {"type": segment_type}
        data = segment.get("data")
        fields = allowed_data_fields.get(segment_type, ())
        if isinstance(data, dict) and fields:
            safe_data = {
                field: data[field]
                for field in fields
                if data.get(field) is not None
            }
            if safe_data:
                item["data"] = safe_data
        if segment_type == "image":
            for field in ("file_hash", "description", "downloaded"):
                if segment.get(field) is not None:
                    item[field] = segment[field]
        safe.append(item)
    return safe


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "INGEST_FAILURE_EVENT_TYPE",
    "IngestFailureDetail",
    "build_ingest_failure_event",
]

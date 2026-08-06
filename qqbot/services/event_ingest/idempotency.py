"""Idempotency keys for NapCat-triggered ingest terminal events.

Contract: 开发文档/v2.0/20-横切契约/EventIngest契约.md §7.2
"""

from __future__ import annotations

from typing import Any


def for_message(self_id: int, message_id: int | str) -> str:
    return f"{self_id}:msg:{message_id}"


def for_notice(
    self_id: int,
    notice_type: str,
    sub_type: str | None,
    time: int,
    user_id: int | None,
    operator_id: int | None,
) -> str:
    return (
        f"{self_id}:notice:{notice_type}:{sub_type or '_'}"
        f":{time}:{user_id or '_'}:{operator_id or '_'}"
    )


def for_recall(self_id: int, message_id: int | str, time: int) -> str:
    return f"{self_id}:recall:{message_id}:{time}"


def for_request(self_id: int, request_type: str, flag: str) -> str:
    return f"{self_id}:request:{request_type}:{flag}"


def for_lifecycle(self_id: int, sub_type: str, time: int) -> str:
    return f"{self_id}:lifecycle:{sub_type}:{time}"


def for_unknown(
    self_id: int,
    post_type: str | None,
    sub_type: str | None,
    time: int,
    user_id: int | None,
) -> str:
    """Fallback identity for an event whose protocol shape is unknown.

    ``runtime.event_ingest_failed`` may carry this key even though its origin is
    runtime: the triggering fact came from NapCat and can be redelivered.
    """
    return (
        f"{self_id}:unknown:{post_type or '_'}:{sub_type or '_'}"
        f":{time}:{user_id or '_'}"
    )


def for_ingest_failure(event: Any) -> str:
    """Best-effort stable identity for a raw event that failed preprocessing."""
    self_id = _int_or_zero(getattr(event, "self_id", 0))
    post_type = getattr(event, "post_type", None)
    time_value = _int_or_zero(getattr(event, "time", 0))

    message_id = getattr(event, "message_id", None)
    if post_type in ("message", "message_sent") and message_id is not None:
        return for_message(self_id, message_id)

    if post_type == "request":
        request_type = str(getattr(event, "request_type", None) or "unknown")
        flag = getattr(event, "flag", None)
        if flag is not None and str(flag):
            return for_request(self_id, request_type, str(flag))

    if (
        post_type == "meta_event"
        and getattr(event, "meta_event_type", None) == "lifecycle"
    ):
        sub_type = str(getattr(event, "sub_type", None) or "unknown")
        return for_lifecycle(self_id, sub_type, time_value)

    if post_type == "notice":
        notice_type = str(getattr(event, "notice_type", None) or "unknown")
        if notice_type in ("group_recall", "friend_recall") and message_id is not None:
            return for_recall(self_id, message_id, time_value)
        return for_notice(
            self_id,
            notice_type,
            getattr(event, "sub_type", None),
            time_value,
            _optional_int(getattr(event, "user_id", None)),
            _optional_int(getattr(event, "operator_id", None)),
        )

    return for_unknown(
        self_id,
        str(post_type) if post_type is not None else None,
        str(getattr(event, "sub_type", None) or "") or None,
        time_value,
        _optional_int(getattr(event, "user_id", None)),
    )


def _int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

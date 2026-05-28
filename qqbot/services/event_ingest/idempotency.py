"""Idempotency key construction for external events.

Contract: 开发文档/v2.0/EventIngest契约.md §4.1
"""

from __future__ import annotations


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

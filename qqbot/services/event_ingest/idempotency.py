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


def for_unknown(
    self_id: int,
    post_type: str | None,
    sub_type: str | None,
    time: int,
    user_id: int | None,
) -> str:
    """§8 未知事件兜底（runtime.napcat_unknown_event）。

    runtime 系里唯一带 idempotency_key 的事件——它虽以 runtime origin 落库，
    但由外部报文触发，napcat 断线重推时同样会重复到达，需要数据库层去重。
    """
    return (
        f"{self_id}:unknown:{post_type or '_'}:{sub_type or '_'}"
        f":{time}:{user_id or '_'}"
    )

"""SystemEvent value objects.

Contract: 开发文档/v2.0/事件系统设计.md §2
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from qqbot.core.ids import new_event_id

Origin = Literal["external", "agent", "runtime"]
Scope = Literal["system", "group", "private"]
Visibility = Literal["agent_visible", "runtime_only"]


@dataclass(frozen=True)
class PartialSystemEvent:
    """Mapper output: a SystemEvent without ids, timestamps, or causality links."""

    origin: Origin
    type: str
    scope: Scope
    group_id: int | None
    user_id: int | None
    visibility: Visibility
    payload: dict
    raw: dict | None
    idempotency_key: str | None


@dataclass(frozen=True)
class SystemEvent:
    """Fully-formed event ready to be persisted into agent_events."""

    event_id: str
    occurred_at: datetime
    origin: str
    type: str
    scope: str
    group_id: int | None
    user_id: int | None
    visibility: str
    correlation_id: str | None
    causation_id: str | None
    idempotency_key: str | None
    payload: dict
    raw: dict | None


def finalize(
    partial: PartialSystemEvent,
    *,
    occurred_at: datetime,
) -> SystemEvent:
    """Stamp a PartialSystemEvent with event_id and self-correlation.

    External events are self-correlated: their correlation_id equals their own
    event_id, so any tick the loop runs in response can reuse it.
    See 事件系统设计.md §6.
    """
    eid = new_event_id()
    return SystemEvent(
        event_id=eid,
        occurred_at=occurred_at,
        origin=partial.origin,
        type=partial.type,
        scope=partial.scope,
        group_id=partial.group_id,
        user_id=partial.user_id,
        visibility=partial.visibility,
        correlation_id=eid,
        causation_id=None,
        idempotency_key=partial.idempotency_key,
        payload=partial.payload,
        raw=partial.raw,
    )

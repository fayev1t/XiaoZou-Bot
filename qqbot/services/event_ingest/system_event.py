"""SystemEvent value objects.

Contract: 开发文档/v2.0/20-横切契约/事件系统设计.md §2
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
    """Pre-finalization ingest event without ids, timestamps, or causality."""

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
    event_id: str | None = None,
) -> SystemEvent:
    """Stamp a PartialSystemEvent with event_id and self-correlation.

    ``event_id`` is used as-is when the caller minted it at arrival; omitted
    means mint here. EventIngest stamps before any await so success and
    failure terminals share that id — media/VLM duration must not reorder
    same-second facts in the projection.

    Ingest terminal events are self-correlated: their correlation_id equals
    their own event_id, so any tick the loop runs in response can reuse it.
    See 事件系统设计.md §6.
    """
    eid = event_id or new_event_id()
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

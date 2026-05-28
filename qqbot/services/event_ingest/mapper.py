"""EventMapper protocol and in-process registry.

Contract: 开发文档/v2.0/EventIngest契约.md §3
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from qqbot.services.event_ingest.system_event import PartialSystemEvent


@runtime_checkable
class EventMapper(Protocol):
    """Map a napcat/onebot event to a PartialSystemEvent.

    Mappers should be cheap, deterministic, and side-effect free.
    Media downloads (image, audio) are not the mapper's responsibility;
    they happen later in the ingest pipeline (EventIngest契约.md §6).
    """

    post_type: str
    sub_type: str | None  # None = fallback within this post_type

    def can_map(self, event: Any) -> bool: ...

    def map(self, event: Any) -> PartialSystemEvent: ...


class MapperRegistry:
    """In-process registry for EventMapper instances.

    Lookup order (EventIngest契约.md §3):
    1. Exact mappers (sub_type is not None) reporting can_map() = True.
    2. Fallback mappers (sub_type is None) only if no exact match.
    """

    def __init__(self) -> None:
        self._mappers: list[EventMapper] = []

    def register(self, mapper: EventMapper) -> None:
        self._mappers.append(mapper)

    def find(self, event: Any) -> EventMapper | None:
        exacts: list[EventMapper] = []
        fallbacks: list[EventMapper] = []
        for mapper in self._mappers:
            if not mapper.can_map(event):
                continue
            if mapper.sub_type is None:
                fallbacks.append(mapper)
            else:
                exacts.append(mapper)
        if exacts:
            return exacts[0]
        if fallbacks:
            return fallbacks[0]
        return None

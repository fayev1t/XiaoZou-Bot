"""EventIngest: v2 ingress layer.

Contracts:
- 开发文档/v2.0/20-横切契约/EventIngest契约.md
- 开发文档/v2.0/20-横切契约/事件系统设计.md

Every non-heartbeat NapCat input is reduced to exactly one committed terminal
event: either the mapped ``external.*`` fact or
``runtime.event_ingest_failed`` when required preprocessing cannot finish.
"""

from qqbot.services.event_ingest.failure import (
    INGEST_FAILURE_EVENT_TYPE,
    IngestFailureDetail,
)
from qqbot.services.event_ingest.ingest import EventIngest, IngestResult
from qqbot.services.event_ingest.mapper import EventMapper, MapperRegistry
from qqbot.services.event_ingest.system_event import (
    PartialSystemEvent,
    SystemEvent,
    finalize,
)

__all__ = [
    "EventIngest",
    "IngestResult",
    "INGEST_FAILURE_EVENT_TYPE",
    "IngestFailureDetail",
    "EventMapper",
    "MapperRegistry",
    "PartialSystemEvent",
    "SystemEvent",
    "finalize",
]

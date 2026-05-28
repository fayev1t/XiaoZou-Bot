"""EventIngest: v2 ingress layer.

Contracts:
- 开发文档/v2.0/EventIngest契约.md (pipeline, mappers, idempotency)
- 开发文档/v2.0/事件系统设计.md (SystemEvent schema, scopes, visibility)

第 2 步范围：mapper + finalize + persist。
尚未实现：媒体下载 (§6)、heartbeat 旁路 (§7)、唤醒 LoopSupervisor (§5)。
"""

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
    "EventMapper",
    "MapperRegistry",
    "PartialSystemEvent",
    "SystemEvent",
    "finalize",
]

"""EventIngest orchestrator: nonebot Event → SystemEvent → agent_events.

Contract: 开发文档/v2.0/EventIngest契约.md §2

Pipeline:
  (0) heartbeat short-circuit → write_heartbeat() (§7.1)
  (1) mapper lookup (§3)；no mapper → runtime.napcat_unknown_event 兜底落库 (§8)
  (2) PartialSystemEvent
  (3) attach_media_to_payload — image download side effects (§6)
  (4) finalize (event_id, occurred_at, self-correlation)
  (5) persist (ON CONFLICT DO NOTHING)
  (6) supervisor.wake(scope_key) if a supervisor was injected (§5)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from qqbot.core.logging import get_logger
from qqbot.core.time import normalize_china_time
from qqbot.services.event_ingest.heartbeat import write_heartbeat
from qqbot.services.event_ingest.idempotency import for_unknown
from qqbot.services.event_ingest.mapper import MapperRegistry
from qqbot.services.event_ingest.media import attach_media_to_payload
from qqbot.services.event_ingest.napcat_helpers import dump_event
from qqbot.services.event_ingest.persistence import persist_event
from qqbot.services.event_ingest.system_event import (
    PartialSystemEvent,
    SystemEvent,
    finalize,
)

logger = get_logger(__name__)

IngestStatus = Literal["inserted", "duplicate", "unknown", "error", "heartbeat"]
SessionFactory = Callable[[], AsyncSession]


@dataclass(frozen=True)
class IngestResult:
    status: IngestStatus
    event: SystemEvent | None = None
    reason: str | None = None


class EventIngest:
    """Single entry point for external events.

    Stateless aside from its registry and session factory. Safe to call
    concurrently as long as the session factory yields independent sessions.
    """

    def __init__(
        self,
        registry: MapperRegistry,
        session_factory: SessionFactory,
        supervisor: Any | None = None,
    ) -> None:
        self._registry = registry
        self._session_factory = session_factory
        # supervisor 是可选注入；骨架前阶段为 None，影子接入时由 plugin
        # 决定是否注入 LoopSupervisor。EventIngest 仅依赖 .wake(scope_key)
        # 这一接口，刻意不做静态类型耦合（避免 ingest 反向依赖 agent_loop）。
        self._supervisor = supervisor

    async def ingest(self, event: Any) -> IngestResult:
        # heartbeat 旁路：不入库，仅原子写 runtime_data/napcat_heartbeat.json
        # 见 EventIngest契约.md §7.1。
        if (
            getattr(event, "post_type", None) == "meta_event"
            and getattr(event, "meta_event_type", None) == "heartbeat"
        ):
            await write_heartbeat(event)
            return IngestResult(status="heartbeat")

        mapper = self._registry.find(event)
        if mapper is None:
            logger.warning(
                "[event_ingest] no mapper matched: post_type={} sub_type={}",
                getattr(event, "post_type", "?"),
                getattr(event, "sub_type", "?"),
            )
            return await self._ingest_unknown(event)

        partial: PartialSystemEvent = mapper.map(event)

        # 媒体副作用：图片同步下载、sha256、本地落盘并就地补充 payload.segments
        # 中 image 段的 file_hash/local_path/downloaded 等字段。见 EventIngest契约.md §6。
        # frozen dataclass 不阻止 dict 字段被 in-place 修改。
        await attach_media_to_payload(partial.payload)

        occurred_at = normalize_china_time(getattr(event, "time", None))
        sys_event = finalize(partial, occurred_at=occurred_at)

        try:
            async with self._session_factory() as session:
                inserted = await persist_event(session, sys_event)
        except Exception as exc:
            logger.error(
                "[event_ingest] persist failed: type={} err={}",
                sys_event.type,
                exc,
            )
            return IngestResult(status="error", event=sys_event, reason=str(exc))

        if not inserted:
            logger.info(
                "[event_ingest] duplicate skipped: type={} key={}",
                sys_event.type,
                sys_event.idempotency_key,
            )
            return IngestResult(status="duplicate", event=sys_event)

        await self._maybe_wake(sys_event)
        return IngestResult(status="inserted", event=sys_event)

    async def _ingest_unknown(self, event: Any) -> IngestResult:
        """没有 mapper 的事件不丢弃——折成 runtime.napcat_unknown_event 落库。

        契约 EventIngest契约.md §8 / 事件系统设计.md §4.3.2：napcat 协议升级、
        第三方扩展推来的未识别报文要留痕并让 SystemAgentLoop 看到（投影层对
        agent_visible 的 runtime.* 泛化渲染成 <system-hint>，无需改动）。
        origin=runtime——协议外报文是基础设施观察，不是可信业务事件；raw 列
        按 §3 只给 external 写，原报文全量放 payload.raw。status 仍返回
        "unknown"（语义是"无 mapper"，供 v2_main 日志区分），但 event 已入库。
        """
        post_type = getattr(event, "post_type", None)
        sub_type = getattr(event, "sub_type", None)
        partial = PartialSystemEvent(
            origin="runtime",
            type="runtime.napcat_unknown_event",
            scope="system",
            group_id=None,
            user_id=getattr(event, "user_id", None),
            visibility="agent_visible",
            payload={
                "post_type": post_type,
                "sub_type": sub_type,
                "raw": dump_event(event),
            },
            raw=None,
            idempotency_key=for_unknown(
                getattr(event, "self_id", 0),
                post_type,
                sub_type,
                int(getattr(event, "time", 0) or 0),
                getattr(event, "user_id", None),
            ),
        )
        occurred_at = normalize_china_time(getattr(event, "time", None))
        sys_event = finalize(partial, occurred_at=occurred_at)

        try:
            async with self._session_factory() as session:
                inserted = await persist_event(session, sys_event)
        except Exception as exc:
            logger.error(
                "[event_ingest] unknown-event persist failed: post_type={} err={}",
                post_type,
                exc,
            )
            return IngestResult(status="error", event=sys_event, reason=str(exc))

        if not inserted:
            return IngestResult(status="duplicate", event=sys_event)

        await self._maybe_wake(sys_event)
        return IngestResult(status="unknown", event=sys_event, reason="no_mapper")

    async def _maybe_wake(self, event: SystemEvent) -> None:
        if self._supervisor is None:
            return
        scope_key = _scope_key_for_wake(event)
        if scope_key is None:
            return
        try:
            await self._supervisor.wake(scope_key)
        except Exception as exc:
            logger.warning("[event_ingest] supervisor.wake failed: {}", exc)


def _scope_key_for_wake(event: SystemEvent) -> str | None:
    """Translate event scope → scope_key understood by LoopSupervisor.

    private events never wake a loop in v2 第一版 (实例化策略 §10.1).
    """
    if event.scope == "group" and event.group_id is not None:
        return f"group:{event.group_id}"
    if event.scope == "system":
        return "system"
    return None

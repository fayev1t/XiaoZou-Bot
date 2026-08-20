"""EventIngest: NapCat 适配 + 接到统一入口网关 / 注册器。

契约：开发文档/v2.0/20-横切契约/提案-重新设计agent_loop前后的模块以及流水线.md
以及 EventIngest契约.md。

heartbeat 仍旁路。其余上游（含模型/工具响应）走：
  入口网关盖 occurred_at → raw 插入 → 注册器 1s 聚水 → 适配 → 排序发 id。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from qqbot.core.logging import get_logger
from qqbot.services.event_gateway.inbound import InboundGateway, UpstreamEnvelope
from qqbot.services.event_gateway.registry import AdaptedEvent, EventRegistrar
from qqbot.services.event_ingest.failure import (
    IngestFailureDetail,
    build_ingest_failure_event,
)
from qqbot.services.event_ingest.heartbeat import write_heartbeat
from qqbot.services.event_ingest.mapper import MapperRegistry
from qqbot.services.event_ingest.media import (
    BatchImageDescriber,
    ImageDescriber,
    attach_media_to_payload,
)
from qqbot.services.event_ingest.napcat_helpers import dump_event
from qqbot.services.event_ingest.persistence import persist_event
from qqbot.services.event_ingest.system_event import (
    PartialSystemEvent,
    SystemEvent,
)

logger = get_logger(__name__)

IngestStatus = Literal[
    "inserted",
    "duplicate",
    "processing_failed",
    "error",
    "heartbeat",
]
SessionFactory = Callable[[], AsyncSession]
CommittedNotifier = Callable[[SystemEvent], Awaitable[None]]


@dataclass(frozen=True)
class IngestResult:
    status: IngestStatus
    event: SystemEvent | None = None
    reason: str | None = None


class EventIngest:
    """NapCat 适配器挂在统一网关上。决策拍不在这里。"""

    def __init__(
        self,
        registry: MapperRegistry,
        session_factory: SessionFactory,
        committed_notifier: CommittedNotifier | None = None,
        image_describer: ImageDescriber | None = None,
        batch_image_describer: BatchImageDescriber | None = None,
        registration_window_seconds: float | None = None,
    ) -> None:
        self._registry = registry
        self._session_factory = session_factory
        self._committed_notifier = committed_notifier
        self._image_describer = image_describer
        self._batch_image_describer = batch_image_describer
        self._registrar = EventRegistrar(
            adapter=self._adapt,
            persist=self._persist_terminal,
            window_seconds=registration_window_seconds,
        )
        self._gateway = InboundGateway(
            session_factory=session_factory,
            registrar=self._registrar,
        )

    @property
    def gateway(self) -> InboundGateway:
        return self._gateway

    async def ingest(self, event: Any) -> IngestResult:
        if (
            getattr(event, "post_type", None) == "meta_event"
            and getattr(event, "meta_event_type", None) == "heartbeat"
        ):
            await write_heartbeat(event)
            return IngestResult(status="heartbeat")

        payload = dump_event(event)
        if not payload:
            payload = {"_repr": repr(event)}
        return await self._gateway.submit(
            "external",
            payload,
            source=event,
        )

    async def ingest_channel(
        self,
        channel: str,
        payload: dict[str, Any],
        *,
        source: Any = None,
    ) -> IngestResult:
        return await self._gateway.submit(channel, payload, source=source)

    async def _adapt(self, envelope: UpstreamEnvelope) -> AdaptedEvent:
        channel = envelope.channel
        if channel == "external":
            return await self._adapt_napcat(envelope.source)
        if channel == "model":
            return self._adapt_model(envelope.payload)
        if channel == "tool":
            return self._adapt_tool(envelope.payload)
        return self._adapt_other(envelope)

    async def _adapt_napcat(self, event: Any) -> AdaptedEvent:
        try:
            mapper = self._registry.find(event)
        except Exception as exc:
            logger.warning("[event_ingest] mapper lookup failed: {}", exc)
            return self._failure(
                event,
                (
                    IngestFailureDetail(
                        stage="event_mapping",
                        error_code="mapper_lookup_failed",
                        reason="事件映射器查找失败",
                    ),
                ),
            )
        if mapper is None:
            logger.warning(
                "[event_ingest] no mapper matched: post_type={} sub_type={}",
                getattr(event, "post_type", "?"),
                getattr(event, "sub_type", "?"),
            )
            return self._failure(
                event,
                (
                    IngestFailureDetail(
                        stage="event_mapping",
                        error_code="no_mapper",
                        reason="未识别的 NapCat 事件类型",
                    ),
                ),
            )

        try:
            partial: PartialSystemEvent = mapper.map(event)
        except Exception as exc:
            logger.warning(
                "[event_ingest] mapper failed: mapper={} err={}",
                type(mapper).__name__,
                exc,
            )
            return self._failure(
                event,
                (
                    IngestFailureDetail(
                        stage="event_mapping",
                        error_code="mapper_failed",
                        reason="NapCat 事件格式化失败",
                    ),
                ),
            )

        try:
            media_result = await attach_media_to_payload(
                partial.payload,
                self._image_describer,
                batch_describer=self._batch_image_describer,
            )
        except Exception as exc:
            logger.warning("[event_ingest] media preprocessing failed: {}", exc)
            return self._failure(
                event,
                (
                    IngestFailureDetail(
                        stage="media_processing",
                        error_code="media_processing_failed",
                        reason="媒体前置处理失败",
                    ),
                ),
                partial=partial,
            )
        if media_result.failures:
            return self._failure(event, media_result.failures, partial=partial)

        return AdaptedEvent(partial=partial, status="inserted")

    def _adapt_model(self, payload: dict[str, Any]) -> AdaptedEvent:
        if not isinstance(payload, dict) or "ok" not in payload:
            return AdaptedEvent(
                partial=PartialSystemEvent(
                    origin="runtime",
                    type="runtime.model_responded",
                    scope="system",
                    group_id=None,
                    user_id=None,
                    visibility="runtime_only",
                    payload={"ok": False, "error_kind": "invalid_shape"},
                    raw=payload if isinstance(payload, dict) else {},
                    idempotency_key=None,
                ),
                status="processing_failed",
                reason="invalid_shape",
            )
        return AdaptedEvent(
            partial=PartialSystemEvent(
                origin="runtime",
                type="runtime.model_responded",
                scope=str(payload.get("scope") or "system"),
                group_id=_optional_int(payload.get("group_id")),
                user_id=_optional_int(payload.get("user_id")),
                visibility="runtime_only",
                payload=dict(payload),
                raw=dict(payload),
                idempotency_key=None,
            ),
            status="inserted" if payload.get("ok") else "processing_failed",
            reason=(
                None
                if payload.get("ok")
                else str(payload.get("error_kind") or "model_failed")
            ),
        )

    def _adapt_tool(self, payload: dict[str, Any]) -> AdaptedEvent:
        if not isinstance(payload, dict):
            payload = {}
        return AdaptedEvent(
            partial=PartialSystemEvent(
                origin="runtime",
                type="runtime.tool_responded",
                scope=str(payload.get("scope") or "system"),
                group_id=_optional_int(payload.get("group_id")),
                user_id=_optional_int(payload.get("user_id")),
                visibility="runtime_only",
                payload=dict(payload),
                raw=dict(payload),
                idempotency_key=None,
            ),
            status="inserted",
        )

    def _adapt_other(self, envelope: UpstreamEnvelope) -> AdaptedEvent:
        payload = envelope.payload if isinstance(envelope.payload, dict) else {}
        event_type = str(payload.get("event_type") or "")
        if event_type == "runtime.silence_elapsed":
            scope = payload.get("scope") or "group"
            if scope not in ("system", "group", "private"):
                scope = "group"
            visibility = payload.get("visibility") or "agent_visible"
            if visibility not in ("agent_visible", "runtime_only"):
                visibility = "agent_visible"
            seconds = payload.get("seconds")
            return AdaptedEvent(
                partial=PartialSystemEvent(
                    origin="runtime",
                    type="runtime.silence_elapsed",
                    scope=scope,
                    group_id=_optional_int(payload.get("group_id")),
                    user_id=_optional_int(payload.get("user_id")),
                    visibility=visibility,
                    payload={"seconds": seconds},
                    raw=dict(payload),
                    idempotency_key=None,
                ),
                status="inserted",
            )
        return AdaptedEvent(
            partial=PartialSystemEvent(
                origin="runtime",
                type="runtime.other_event",
                scope="system",
                group_id=None,
                user_id=None,
                visibility="runtime_only",
                payload=dict(payload),
                raw=dict(payload),
                idempotency_key=None,
            ),
            status="inserted",
        )

    def _failure(
        self,
        event: Any,
        failures: tuple[IngestFailureDetail, ...],
        *,
        partial: PartialSystemEvent | None = None,
    ) -> AdaptedEvent:
        return AdaptedEvent(
            partial=build_ingest_failure_event(event, failures, partial=partial),
            status="processing_failed",
            reason=failures[0].error_code,
        )

    async def _persist_terminal(
        self,
        sys_event: SystemEvent,
        inserted_status: str,
        reason: str | None,
    ) -> IngestResult:
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

        await self._notify_committed(sys_event)
        status: IngestStatus = (
            "processing_failed"
            if inserted_status == "processing_failed"
            else "inserted"
        )
        return IngestResult(status=status, event=sys_event, reason=reason)

    async def _notify_committed(self, event: SystemEvent) -> None:
        if self._committed_notifier is None:
            return
        try:
            await self._committed_notifier(event)
        except Exception as exc:
            logger.warning("[event_ingest] committed notifier failed: {}", exc)


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

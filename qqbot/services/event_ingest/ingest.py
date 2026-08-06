"""EventIngest orchestrator: NapCat input → one terminal SystemEvent.

Contract: 开发文档/v2.0/20-横切契约/EventIngest契约.md §3

Pipeline:
  (0) heartbeat short-circuit → write_heartbeat() (§9)
  (1) mapper lookup + mapping (§4)
  (2) required content preprocessing, including image persistence + VLM (§5)
  (3) choose exactly one terminal event:
      success → mapper's external.* event
      failure → runtime.event_ingest_failed
  (4) finalize + persist (ON CONFLICT DO NOTHING)
  (5) notify the committed internal event; only this ingest notification may
      wake AgentLoop for the current NapCat input
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from qqbot.core.logging import get_logger
from qqbot.core.time import china_now, normalize_china_time
from qqbot.services.event_ingest.failure import (
    IngestFailureDetail,
    build_ingest_failure_event,
)
from qqbot.services.event_ingest.heartbeat import write_heartbeat
from qqbot.services.event_ingest.mapper import MapperRegistry
from qqbot.services.event_ingest.media import ImageDescriber, attach_media_to_payload
from qqbot.services.event_ingest.persistence import persist_event
from qqbot.services.event_ingest.system_event import (
    PartialSystemEvent,
    SystemEvent,
    finalize,
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
    """Single entry point for external events.

    Stateless aside from its registry and session factory. Safe to call
    concurrently as long as the session factory yields independent sessions.
    """

    def __init__(
        self,
        registry: MapperRegistry,
        session_factory: SessionFactory,
        committed_notifier: CommittedNotifier | None = None,
        image_describer: ImageDescriber | None = None,
    ) -> None:
        self._registry = registry
        self._session_factory = session_factory
        # EventIngest 只发布“内部事件已提交”这一事实，不认识 LoopSupervisor、
        # scope_key 或 wake 模式。生产装配在 plugin 层把该通知翻译为 AgentLoop wake。
        self._committed_notifier = committed_notifier
        # 看图写客观描述的回调（2026-07-28）。以鸭子类型注入，保持 ingest
        # 不静态依赖 agent_loop；含图片却未注入时会形成处理失败内部事件。
        # 生产实现在 agent_loop.image_description，由 v2_main 绑好 session_factory
        # 传进来。
        self._image_describer = image_describer

    async def ingest(self, event: Any) -> IngestResult:
        # heartbeat 旁路：不入库，仅原子写 runtime_data/napcat_heartbeat.json
        # 见 EventIngest契约.md §9。
        if (
            getattr(event, "post_type", None) == "meta_event"
            and getattr(event, "meta_event_type", None) == "heartbeat"
        ):
            await write_heartbeat(event)
            return IngestResult(status="heartbeat")

        try:
            mapper = self._registry.find(event)
        except Exception as exc:
            logger.warning("[event_ingest] mapper lookup failed: {}", exc)
            return await self._commit_failure(
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
            return await self._commit_failure(
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
            return await self._commit_failure(
                event,
                (
                    IngestFailureDetail(
                        stage="event_mapping",
                        error_code="mapper_failed",
                        reason="NapCat 事件格式化失败",
                    ),
                ),
            )

        # 媒体副作用：图片同步下载、sha256、本地落盘并就地补充
        # payload.segments 中的 file_hash/local_path/downloaded 等字段。
        # 2026-07-28 起同一步里还会调 VLM 写 description（Planner 不直接接收
        # 图片像素，描述是正常消息进入时间线的必需内容）。frozen dataclass 不阻止
        # dict 字段被 in-place 修改。
        try:
            media_result = await attach_media_to_payload(
                partial.payload,
                self._image_describer,
            )
        except Exception as exc:
            logger.warning("[event_ingest] media preprocessing failed: {}", exc)
            return await self._commit_failure(
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
            return await self._commit_failure(
                event,
                media_result.failures,
                partial=partial,
            )

        try:
            sys_event = self._finalize(event, partial)
        except Exception as exc:
            logger.warning("[event_ingest] event finalization failed: {}", exc)
            return await self._commit_failure(
                event,
                (
                    IngestFailureDetail(
                        stage="event_finalization",
                        error_code="event_finalization_failed",
                        reason="内部事件定稿失败",
                    ),
                ),
                partial=partial,
            )
        return await self._persist_terminal(sys_event, inserted_status="inserted")

    async def _commit_failure(
        self,
        event: Any,
        failures: tuple[IngestFailureDetail, ...],
        *,
        partial: PartialSystemEvent | None = None,
    ) -> IngestResult:
        failure_partial = build_ingest_failure_event(
            event,
            failures,
            partial=partial,
        )
        try:
            sys_event = self._finalize(event, failure_partial)
        except Exception as exc:
            logger.warning(
                "[event_ingest] failure timestamp invalid; using receive time: {}",
                exc,
            )
            sys_event = finalize(failure_partial, occurred_at=china_now())
        return await self._persist_terminal(
            sys_event,
            inserted_status="processing_failed",
            reason=failures[0].error_code,
        )

    @staticmethod
    def _finalize(event: Any, partial: PartialSystemEvent) -> SystemEvent:
        occurred_at = normalize_china_time(getattr(event, "time", None))
        return finalize(partial, occurred_at=occurred_at)

    async def _persist_terminal(
        self,
        sys_event: SystemEvent,
        *,
        inserted_status: Literal["inserted", "processing_failed"],
        reason: str | None = None,
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
        return IngestResult(
            status=inserted_status,
            event=sys_event,
            reason=reason,
        )

    async def _notify_committed(self, event: SystemEvent) -> None:
        if self._committed_notifier is None:
            return
        try:
            await self._committed_notifier(event)
        except Exception as exc:
            logger.warning("[event_ingest] committed notifier failed: {}", exc)

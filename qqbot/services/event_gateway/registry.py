"""统一事件注册器。

被事件激活后收集 1s，窗口内并发走适配器，完成后再按 occurred_at（同刻用
seq）排序，然后发 event_id / 哈希并落事件流。不注入 register_at。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from qqbot.core.ids import new_event_id
from qqbot.core.logging import get_logger
from qqbot.services.event_ingest.system_event import (
    PartialSystemEvent,
    SystemEvent,
    finalize,
)

logger = get_logger(__name__)

# 生产由 EventIngest(..., registration_window_seconds=1.0) 打开。
# 契约测试默认 0，避免每条 ingest 空等一秒。
_REGISTRATION_WINDOW_SECONDS = 0.0

AdaptFn = Callable[[Any], Awaitable["AdaptedEvent"]]
PersistFn = Callable[[SystemEvent, str, str | None], Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class AdaptedEvent:
    """适配器产物：尚未发身份的 partial。"""

    partial: PartialSystemEvent
    status: Literal["inserted", "processing_failed"]
    reason: str | None = None


class EventRegistrar:
    """全局一只窗口，不分群。"""

    def __init__(
        self,
        *,
        adapter: AdaptFn,
        persist: PersistFn,
        window_seconds: float | None = None,
    ) -> None:
        self._adapter = adapter
        self._persist = persist
        self._window = (
            _REGISTRATION_WINDOW_SECONDS
            if window_seconds is None
            else float(window_seconds)
        )
        self._buffer: list[Any] = []
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._seq = 0

    def allocate_seq(self) -> int:
        self._seq += 1
        return self._seq

    async def enqueue(self, envelope: Any) -> Any:
        async with self._lock:
            self._buffer.append(envelope)
            if self._task is None:
                self._task = asyncio.create_task(self._run_window())
        return await envelope.future

    async def _run_window(self) -> None:
        delay = self._window
        if delay > 0:
            await asyncio.sleep(delay)
        async with self._lock:
            batch = self._buffer
            self._buffer = []
            self._task = None
        if not batch:
            return
        try:
            adapted = await asyncio.gather(
                *[self._adapt_one(item) for item in batch],
            )
            ordered = sorted(
                zip(batch, adapted, strict=True),
                key=lambda pair: (pair[0].occurred_at, pair[0].seq),
            )
            for envelope, item in ordered:
                await self._register_one(envelope, item)
        except Exception as exc:
            logger.warning("[event_registry] window crashed err={}", exc)
            from qqbot.services.event_ingest.ingest import IngestResult

            for envelope in batch:
                if not envelope.future.done():
                    envelope.future.set_result(
                        IngestResult(status="error", reason=str(exc))
                    )

    async def _adapt_one(self, envelope: Any) -> AdaptedEvent:
        try:
            return await self._adapter(envelope)
        except Exception as exc:
            logger.warning(
                "[event_registry] adapter crashed channel={} err={}",
                getattr(envelope, "channel", "?"),
                exc,
            )
            from qqbot.services.event_ingest.failure import (
                IngestFailureDetail,
                build_ingest_failure_event,
            )

            source = envelope.source if envelope.source is not None else envelope
            partial = build_ingest_failure_event(
                source,
                (
                    IngestFailureDetail(
                        stage="event_mapping",
                        error_code="adapter_failed",
                        reason="事件适配失败",
                    ),
                ),
            )
            return AdaptedEvent(
                partial=partial,
                status="processing_failed",
                reason="adapter_failed",
            )

    async def _register_one(self, envelope: Any, item: AdaptedEvent) -> None:
        from qqbot.services.event_ingest.ingest import IngestResult

        try:
            event_id = new_event_id()
            sys_event = finalize(
                item.partial,
                occurred_at=envelope.occurred_at,
                event_id=event_id,
            )
            result = await self._persist(
                sys_event,
                item.status,
                item.reason,
            )
        except Exception as exc:
            logger.warning("[event_registry] persist crashed err={}", exc)
            result = IngestResult(status="error", reason=str(exc))
        if not envelope.future.done():
            envelope.future.set_result(result)

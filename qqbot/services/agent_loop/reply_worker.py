"""ReplySendWorker — 将 agent.reply_emitted 落到 napcat 真实发出。

设计：混合 push+pull（见 2026-05-26 设计讨论）

  1. 启动时 wake_event 预 set，第一次循环即 catchup 扫描
  2. 平时阻塞在 asyncio.Event 上
  3. AgentLoop 写完 reply_emitted 后调 LoopSupervisor.notify_reply_pending()
     → set wake_event
  4. worker 醒来执行一次 _drain_once()，把所有 pending 全部送完归零

幂等基础：通过 SQL `NOT EXISTS(reply_delivered|reply_failed
WHERE causation_id = reply.event_id)` 判断 pending。重启 / 重入安全；
即使同一时刻多个 worker 实例（理论上不会有）也只会重复发送、不会丢。

契约：任务与决策契约.md §6 (ReplyAction → reply_emitted → delivered/failed)
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from qqbot.core.logging import get_logger
from qqbot.services.agent_loop import bot_registry
from qqbot.services.agent_loop.delivery_claims import (
    DEFAULT_LEASE_SECONDS,
    claim_delivery,
)
from qqbot.services.agent_loop.event_writer import write_agent_event

logger = get_logger(__name__)

SessionFactory = Callable[[], AsyncSession]
_LEASE_RETRY_EPSILON_SECONDS = 0.1

# 一次最多扫 100 条；正常稳态下 pending 队列应当极短。
# 拉满时会立刻再循环一次（_wake 保持 set），不会丢。
_PENDING_QUERY = text(
    """
    SELECT
        event_id,
        scope,
        group_id,
        user_id,
        correlation_id,
        payload
    FROM agent_events r
    WHERE r.type = 'agent.reply_emitted'
      AND NOT EXISTS (
          SELECT 1 FROM agent_events d
          WHERE d.causation_id = r.event_id
            AND d.type IN ('agent.reply_delivered', 'agent.reply_failed')
      )
    ORDER BY r.occurred_at ASC
    LIMIT 100
    """
)


class ReplySendWorker:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory
        self._wake = asyncio.Event()
        self._stopped = False
        self._task: asyncio.Task[None] | None = None
        self._retry_handle: asyncio.TimerHandle | None = None
        self._retry_deadline: float | None = None
        # LIMIT 拉满时仅在本轮真的推进了终态事件才立刻继续扫，避免 live lease
        # 的 pending 在安静 scope 里触发 busy loop。
        self._last_drain_completed = 0

    def notify(self) -> None:
        if self._stopped:
            return
        self._wake.set()

    def start(self) -> None:
        if self._task is not None:
            return
        # 启动即 set 一次 → 第一次循环就做 catchup 扫描，覆盖重启场景。
        self._wake.set()
        self._task = asyncio.create_task(self._run(), name="reply_send_worker")

    async def stop(self) -> None:
        self._stopped = True
        if self._retry_handle is not None:
            self._retry_handle.cancel()
            self._retry_handle = None
            self._retry_deadline = None
        self._wake.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            finally:
                self._task = None

    async def _run(self) -> None:
        logger.info("[reply_worker] started")
        try:
            while not self._stopped:
                await self._wake.wait()
                self._wake.clear()
                if self._stopped:
                    break
                try:
                    scanned = await self._drain_once()
                    if self._last_drain_completed > 0:
                        logger.info(
                            "[reply_worker] processed {} replies",
                            self._last_drain_completed,
                        )
                    # 如果一次拉满了 LIMIT，再 set 一次让下一轮立刻继续
                    if scanned >= 100 and self._last_drain_completed > 0:
                        self._wake.set()
                except Exception as exc:
                    logger.exception("[reply_worker] drain failed: {}", exc)
        finally:
            logger.info("[reply_worker] stopped")

    async def _drain_once(self) -> int:
        async with self._session_factory() as session:
            result = await session.execute(_PENDING_QUERY)
            rows = list(result.mappings().all())

        completed = 0
        for row in rows:
            try:
                if await self._process_one(row):
                    completed += 1
            except Exception as exc:
                logger.exception(
                    "[reply_worker] unexpected error on event_id={}: {}",
                    row.get("event_id"),
                    exc,
                )
        self._last_drain_completed = completed
        return len(rows)

    async def _process_one(self, row: Any) -> bool:
        event_id: str = row["event_id"]
        scope: str = row["scope"]
        group_id: int | None = row["group_id"]
        user_id: int | None = row["user_id"]
        correlation_id: str = row["correlation_id"]
        payload: dict = row["payload"] or {}

        reply_id = payload.get("reply_id")
        content = payload.get("content") or []
        target = payload.get("target") or {}

        scope_key = _scope_key_from_row(scope, group_id, user_id)

        # 出货去重:抢占本条 reply 的投递权,再真正发 napcat。抢不到(并发实例 /
        # 上次尝试仍在租约内)→ 跳过,避免重复发送;事件仍 pending,租约到期后
        # 由延迟 wake 重新扫描,不再依赖下一个外部 notify 才能复活。见
        # delivery_claims.claim_delivery。
        claim = await claim_delivery(self._session_factory, event_id, "reply")
        if not claim.claimed:
            retry_after = (
                claim.retry_after_seconds
                if claim.retry_after_seconds is not None
                else float(DEFAULT_LEASE_SECONDS)
            )
            logger.info(
                "[reply_worker] reply_emitted={} already claimed, retry in {:.1f}s",
                event_id,
                retry_after,
            )
            self._schedule_retry(retry_after)
            return False

        claimed_here = True
        terminal_written = False
        try:
            try:
                bot, onebot_message_id = await self._send(target, content)
            except Exception as exc:
                logger.warning(
                    "[reply_worker] send failed reply_id={} err={}",
                    reply_id,
                    exc,
                )
                await write_agent_event(
                    self._session_factory,
                    event_type="agent.reply_failed",
                    scope_key=scope_key,
                    correlation_id=correlation_id,
                    causation_id=event_id,
                    payload={
                        "reply_id": reply_id,
                        "error_kind": type(exc).__name__,
                        "error_message": str(exc),
                    },
                )
                terminal_written = True
                return True

            await write_agent_event(
                self._session_factory,
                event_type="agent.reply_delivered",
                scope_key=scope_key,
                correlation_id=correlation_id,
                causation_id=event_id,
                payload={
                    "reply_id": reply_id,
                    "onebot_message_id": onebot_message_id,
                    "self_id": str(getattr(bot, "self_id", "") or "") or None,
                },
            )
            terminal_written = True
            return True
        finally:
            # 已持有 lease 但终态事件没写成（最典型是 delivered/failed 落库抖动）
            # 时,当前 scope 可能不会再来新 notify。补一发 lease-expiry retry wake,
            # 避免 quiet scope 下 pending 永久卡死。
            if claimed_here and not terminal_written:
                self._schedule_retry(float(DEFAULT_LEASE_SECONDS))

    def _schedule_retry(self, delay_seconds: float) -> None:
        if self._stopped:
            return
        loop = asyncio.get_running_loop()
        deadline = (
            loop.time()
            + max(delay_seconds, 0.0)
            + _LEASE_RETRY_EPSILON_SECONDS
        )
        if self._retry_handle is not None and not self._retry_handle.cancelled():
            current_deadline = self._retry_deadline or 0.0
            if current_deadline <= deadline:
                return
            self._retry_handle.cancel()
        self._retry_deadline = deadline
        self._retry_handle = loop.call_at(deadline, self._on_retry_deadline)

    def _on_retry_deadline(self) -> None:
        self._retry_handle = None
        self._retry_deadline = None
        if self._stopped:
            return
        self._wake.set()

    async def _send(
        self, target: dict, content: list[dict]
    ) -> tuple[Any, Any]:
        """调 napcat 发出去。返回 (bot 实例, napcat 返回的 message_id)。"""
        kind = target.get("kind")
        self_id = target.get("self_id")

        bot = bot_registry.get(self_id) if self_id else bot_registry.get_any()
        if bot is None:
            raise RuntimeError("no_bot_available")

        if kind == "group":
            group_id = target.get("group_id")
            if not group_id:
                raise ValueError("target.group_id missing for kind=group")
            result = await bot.send_group_msg(
                group_id=int(group_id), message=content
            )
        elif kind == "private":
            user_id = target.get("user_id")
            if not user_id:
                raise ValueError("target.user_id missing for kind=private")
            result = await bot.send_private_msg(
                user_id=int(user_id), message=content
            )
        else:
            raise ValueError(f"unknown target.kind: {kind!r}")

        return bot, _extract_message_id(result)


def _extract_message_id(result: Any) -> Any:
    if isinstance(result, dict):
        return result.get("message_id")
    if isinstance(result, int):
        return result
    return None


def _scope_key_from_row(
    scope: str, group_id: int | None, user_id: int | None
) -> str:
    """事件行的 (scope, group_id, user_id) → scope_key，与 event_writer
    的 parse_scope_key 互为逆函数。
    """
    if scope == "group" and group_id is not None:
        return f"group:{group_id}"
    if scope == "private" and user_id is not None:
        return f"private:{user_id}"
    return "system"

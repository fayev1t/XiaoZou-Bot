"""ToolWorker — 消费 agent.tool_called 调用 ToolRegistry 并写 agent.tool_result/failed。

设计与 ReplySendWorker 同构（push+pull dispatcher，详见 2026-05-26 设计讨论）：

  1. 启动时 wake_event 预 set，第一次循环 catchup 扫描遗留 pending
  2. 平时阻塞在 asyncio.Event 上
  3. AgentLoop 写完 agent.tool_called 后调 LoopSupervisor.notify_tool_pending()
     → set wake_event
  4. worker 醒来执行一次 _drain_once()：单条 SQL 拉所有未结配 tool_called，
     逐条调 registry.run() 后写 tool_result / tool_failed
  5. drain 结束后对本轮处理过的每个 scope_key 调 supervisor.wake() ——
     让 AgentLoop 在工具结果落表后立刻自驱开下一 tick，不再依赖外部消息
     兜底（即拓扑 README §5.3 的 "工具结果不自动驱动下一 tick" 修复）。

幂等：SQL `NOT EXISTS(tool_result|tool_failed WHERE causation_id=tool_called.event_id)`，
重启 / 重入安全。

执行后**不自动**推进任务状态（pending→running 已由 AgentLoop 在写 tool_called
时附带完成；最终 done/failed 由 LLM 通过 complete_task / fail_task 显式驱动）。

契约：任务与决策契约.md §5.1 ToolResultView, §6 ToolCall lifecycle
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from qqbot.core.logging import get_logger
from qqbot.services.agent_loop.event_writer import write_agent_event
from qqbot.services.agent_loop.tool_registry import ToolRegistry

logger = get_logger(__name__)

SessionFactory = Callable[[], AsyncSession]

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
    WHERE r.type = 'agent.tool_called'
      AND NOT EXISTS (
          SELECT 1 FROM agent_events d
          WHERE d.causation_id = r.event_id
            AND d.type IN ('agent.tool_result', 'agent.tool_failed')
      )
    ORDER BY r.occurred_at ASC
    LIMIT 100
    """
)


class ToolWorker:
    def __init__(
        self,
        session_factory: SessionFactory,
        registry: ToolRegistry,
        supervisor: Any | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._registry = registry
        # supervisor 鸭子类型注入：只用到 wake(scope_key) 异步接口；None 时
        # 退化为不自驱（旧测试 / 早期骨架兼容）。
        self._supervisor = supervisor
        self._wake = asyncio.Event()
        self._stopped = False
        self._task: asyncio.Task[None] | None = None

    def notify(self) -> None:
        if self._stopped:
            return
        self._wake.set()

    def start(self) -> None:
        if self._task is not None:
            return
        # 启动即 set 一次 → 第一次循环就做 catchup 扫描，覆盖重启场景。
        self._wake.set()
        self._task = asyncio.create_task(self._run(), name="tool_worker")

    async def stop(self) -> None:
        self._stopped = True
        self._wake.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            finally:
                self._task = None

    async def _run(self) -> None:
        logger.info("[tool_worker] started")
        try:
            while not self._stopped:
                await self._wake.wait()
                self._wake.clear()
                if self._stopped:
                    break
                try:
                    processed = await self._drain_once()
                    if processed > 0:
                        logger.info(
                            "[tool_worker] processed {} tool calls", processed
                        )
                    if processed >= 100:
                        self._wake.set()
                except Exception as exc:
                    logger.exception("[tool_worker] drain failed: {}", exc)
        finally:
            logger.info("[tool_worker] stopped")

    async def _drain_once(self) -> int:
        async with self._session_factory() as session:
            result = await session.execute(_PENDING_QUERY)
            rows = list(result.mappings().all())

        scopes_to_wake: set[str] = set()
        for row in rows:
            try:
                scope_key = await self._process_one(row)
                if scope_key:
                    scopes_to_wake.add(scope_key)
            except Exception as exc:
                logger.exception(
                    "[tool_worker] unexpected error on event_id={}: {}",
                    row.get("event_id"),
                    exc,
                )

        # 自驱下一 tick：每个本批次涉及的 scope 唤醒一次 AgentLoop。
        # 同一 scope 多条 tool_result 合并成单次 wake（asyncio.Event 本身幂等，
        # 这里做去重只是为了日志与下游清晰）。
        if self._supervisor is not None and scopes_to_wake:
            for scope_key in scopes_to_wake:
                try:
                    await self._supervisor.wake(scope_key)
                    logger.info(
                        "[tool_worker] self-wake scope={} after {} tool result(s)",
                        scope_key,
                        len(rows),
                    )
                except Exception as exc:
                    logger.warning(
                        "[tool_worker] supervisor.wake({}) failed: {}",
                        scope_key,
                        exc,
                    )
        return len(rows)

    async def _process_one(self, row: Any) -> str | None:
        event_id: str = row["event_id"]
        scope: str = row["scope"]
        group_id: int | None = row["group_id"]
        user_id: int | None = row["user_id"]
        correlation_id: str = row["correlation_id"]
        payload: dict = row["payload"] or {}

        tool_call_id = payload.get("tool_call_id")
        tool_name = payload.get("tool_name") or ""
        arguments = payload.get("arguments") or {}
        task_id = payload.get("task_id")

        scope_key = _scope_key_from_row(scope, group_id, user_id)
        tool = self._registry.get(tool_name)

        if tool is None:
            await write_agent_event(
                self._session_factory,
                event_type="agent.tool_failed",
                scope_key=scope_key,
                correlation_id=correlation_id,
                causation_id=event_id,
                payload={
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_name,
                    "task_id": task_id,
                    "error_kind": "unknown_tool",
                    "error_message": f"no tool registered with name {tool_name!r}",
                },
            )
            return scope_key

        try:
            # 系统级 context 统一作为 kwargs 注入；每个工具收到的 context 完全
            # 相同，按需消费、不需要的用 **_ 忽略（黑盒：系统只喂 input、收
            # output，不必按名字特判任何工具）。
            #   scope_key / task_id / correlation_id —— 路由与审计
            #   session_factory                       —— 写/查 agent_events
            #     (reply 写 reply_emitted；search_history 查历史)
            #   notify_reply_pending                  —— reply 写完唤醒
            #     ReplySendWorker 立即出货；supervisor 缺失时为 None（catchup 兜底）
            result = await tool.run(
                arguments,
                scope_key=scope_key,
                task_id=task_id,
                correlation_id=correlation_id,
                session_factory=self._session_factory,
                # getattr 兜底：supervisor 鸭子类型注入，可能为 None（早期骨架）
                # 或只实现了部分接口（测试 stub）；取不到回调就传 None，reply
                # 工具据此退化为不主动唤醒（catchup 兜底）。
                notify_reply_pending=getattr(
                    self._supervisor, "notify_reply_pending", None
                ),
            )
        except Exception as exc:
            logger.warning(
                "[tool_worker] {} raised: {}", tool_name, exc
            )
            await write_agent_event(
                self._session_factory,
                event_type="agent.tool_failed",
                scope_key=scope_key,
                correlation_id=correlation_id,
                causation_id=event_id,
                payload={
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_name,
                    "task_id": task_id,
                    "error_kind": type(exc).__name__,
                    "error_message": str(exc)[:1000],
                },
            )
            return scope_key

        await write_agent_event(
            self._session_factory,
            event_type="agent.tool_result",
            scope_key=scope_key,
            correlation_id=correlation_id,
            causation_id=event_id,
            payload={
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "task_id": task_id,
                "result": result,
            },
        )
        return scope_key


def _scope_key_from_row(
    scope: str, group_id: int | None, user_id: int | None
) -> str:
    if scope == "group" and group_id is not None:
        return f"group:{group_id}"
    if scope == "private" and user_id is not None:
        return f"private:{user_id}"
    return "system"

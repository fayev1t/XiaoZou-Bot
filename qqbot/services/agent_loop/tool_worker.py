"""ToolWorker — 执行 worker 模式工具并写 agent.tool_result/failed。

push+pull dispatcher 设计（详见 2026-05-26 设计讨论）：本 worker 派发
Planner 工具；reply_task 的最终发送由独立 ReplyExecutor 负责。

  1. 启动时 wake_event 预 set，第一次循环 catchup 扫描遗留 pending
  2. 平时阻塞在 asyncio.Event 上
  3. AgentLoop 写完 agent.tool_called 后调 LoopSupervisor.notify_tool_pending()
     → set wake_event
  4. worker 醒来执行一次 _drain_once()：单条 SQL 拉所有未结配 tool_called，
     逐条调 registry.run() 后写 tool_result / tool_failed
  5. **批次收口唤醒**：同一 tick 派发的工具带同一 tool_batch_id（+
     tool_batch_size），本轮写完 terminal 后对涉及的每个批次判定"整批是否
     全部 terminal 且条数 ≥ batch_size"——收口了才写一条
     runtime.tool_batch_completed 标记事件，再经
     supervisor.notify_tool_batch_completed 通知并唤醒该 scope **一次**。
     不再是"每 drain 一轮就按 scope wake"（那会让先完成的工具提前
     唤醒下一拍，慢工具还没回来，模型容易复读）。无批次标记的遗留
     tool_called（升级前落库的）维持旧行为：drain 后按 scope 直接 wake。

幂等：SQL `NOT EXISTS(tool_result|tool_failed WHERE
causation_id=tool_called.event_id)`，
重启 / 重入安全；runtime.tool_batch_completed 写前查重（同 batch_id 只写一条），
但已存在时仍会补发完成通知（修复"写了标记、进程在唤醒前挂了"的半截状态）。

批次判定/completion 事件写入/唤醒由共享 ``tool_batch`` 编排层负责——
AgentLoop 的 inline 工具和本 worker 谁最后补齐整批 terminal，谁执行收口；
工具保持黑盒（输入 arguments、返回 ToolOutcome），对批次一无所知。

``execution_mode="inline"`` 的调用由 AgentLoop 在当前 tick 原子写入 called +
领域事件 + terminal，因此不会出现在本 worker 的 pending 查询中。

执行后**不自动**推进任务状态（pending→running 已由 AgentLoop 在写 tool_called
时附带完成；最终 done/failed 由 LLM 通过 inline task 工具显式驱动）。

契约：任务与决策契约.md §5.1 ToolResultView, §6 ToolCall lifecycle
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from qqbot.core.logging import get_logger
from qqbot.services.agent_loop.delivery_claims import (
    DEFAULT_LEASE_SECONDS,
    claim_delivery,
)
from qqbot.services.agent_loop.event_writer import (
    AgentEventWrite,
    write_agent_event,
    write_agent_events,
)
from qqbot.services.agent_loop.tool_batch import maybe_close_tool_batch
from qqbot.services.agent_loop.tool_registry import (
    ToolOutcome,
    ToolRegistry,
    coerce_tool_outcome,
)

logger = get_logger(__name__)

SessionFactory = Callable[[], AsyncSession]
_LEASE_RETRY_EPSILON_SECONDS = 0.1

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
    ORDER BY r.occurred_at ASC, r.event_id ASC
    LIMIT 100
    """
)
# ↑ event_id（ULID 单调）做第二排序键：同拍动作事件的 occurred_at 统一回填为
# 投影时刻（loop._apply_actions，2026-07-27）后时间戳相同，认领次序退化为不
# 定序；ULID 恢复写入相对顺序。


@dataclass(frozen=True)
class _ProcessedCall:
    """_process_one 写完 terminal 后带回 drain 层的批次线索。"""

    scope_key: str
    tool_batch_id: str | None
    tool_batch_size: int | None
    terminal_event_id: str
    correlation_id: str


class ToolWorker:
    def __init__(
        self,
        session_factory: SessionFactory,
        registry: ToolRegistry,
        supervisor: Any | None = None,
        caption_image: Any | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._registry = registry
        # supervisor 鸭子类型注入：只用到 wake(scope_key) 异步接口；None 时
        # 退化为不自驱（旧测试 / 早期骨架兼容）。
        self._supervisor = supervisor
        # 看图写描述回调（async (bytes, mime, note) -> str，生产接
        # meme_caption.caption_image）：meme 工具收录/换描述时用。与
        # session_factory 同一条注入链进 run() context；None 时工具自行降级
        # 失败（与 wait 缺 wake_scope 同式）。
        self._caption_image = caption_image
        self._wake = asyncio.Event()
        self._stopped = False
        self._task: asyncio.Task[None] | None = None
        self._retry_handle: asyncio.TimerHandle | None = None
        self._retry_deadline: float | None = None
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
        self._task = asyncio.create_task(self._run(), name="tool_worker")

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
        logger.info("[tool_worker] started")
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
                            "[tool_worker] processed {} tool calls",
                            self._last_drain_completed,
                        )
                    if scanned >= 100 and self._last_drain_completed > 0:
                        self._wake.set()
                except Exception as exc:
                    logger.exception("[tool_worker] drain failed: {}", exc)
        finally:
            logger.info("[tool_worker] stopped")

    async def _drain_once(self) -> int:
        async with self._session_factory() as session:
            result = await session.execute(_PENDING_QUERY)
            rows = list(result.mappings().all())

        completed = 0
        # (scope_key, tool_batch_id) → 本轮该批次最后一条 _ProcessedCall（其
        # terminal_event_id 作 completion 事件的 causation 锚）。
        touched_batches: dict[tuple[str, str], _ProcessedCall] = {}
        # 无批次标记（升级前落库）的遗留 tool_called 涉及的 scope。
        legacy_scopes: set[str] = set()
        for row in rows:
            try:
                done = await self._process_one(row)
            except Exception as exc:
                logger.exception(
                    "[tool_worker] unexpected error on event_id={}: {}",
                    row.get("event_id"),
                    exc,
                )
                continue
            if done is None:
                continue
            completed += 1
            if done.tool_batch_id:
                key = (done.scope_key, done.tool_batch_id)
                touched_batches[key] = done
            else:
                legacy_scopes.add(done.scope_key)

        # 遗留（无批次标记）：维持旧的"drain 后按 scope 直接唤醒"。即使该
        # scope 同时还有新批次在执行也不推迟；模型会从 processing 行识别现状。
        if self._supervisor is not None:
            for scope_key in sorted(legacy_scopes):
                try:
                    await self._supervisor.wake(scope_key)
                    logger.info(
                        "[tool_worker] self-wake scope={} (legacy unbatched "
                        "tool call)",
                        scope_key,
                    )
                except Exception as exc:
                    logger.warning(
                        "[tool_worker] supervisor.wake({}) failed: {}",
                        scope_key,
                        exc,
                    )

        # 批次收口：整批全部 terminal → 写 runtime.tool_batch_completed →
        # 完成通知 + 批次级唤醒一次。判定放在本轮所有 terminal 都落库之后，保证
        # "唤醒到达时完成事件必已在事件流里"。
        for (scope_key, batch_id), last in touched_batches.items():
            try:
                await maybe_close_tool_batch(
                    self._session_factory,
                    supervisor=self._supervisor,
                    scope_key=scope_key,
                    tool_batch_id=batch_id,
                    tool_batch_size=last.tool_batch_size,
                    terminal_event_id=last.terminal_event_id,
                    correlation_id=last.correlation_id,
                )
            except Exception as exc:
                logger.exception(
                    "[tool_worker] batch close check failed: scope={} "
                    "batch={}: {}",
                    scope_key,
                    batch_id,
                    exc,
                )
        self._last_drain_completed = completed
        return len(rows)

    async def _process_one(self, row: Any) -> _ProcessedCall | None:
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
        # 权限判定下放到工具内（BaseTool.enforce_permission）后，把 AgentLoop
        # 在 dispatch 时解析好、写进 tool_called.payload 的触发用户身份透传进
        # run() 的 context，供工具自判。
        triggered_by_event_id = payload.get("triggered_by_event_id")
        triggered_by_user_tier = payload.get("triggered_by_user_tier")
        bot_role = payload.get("bot_role")
        # 批次线索（编排层自用，不透传给工具）：升级前落库的行没有这两个键，
        # 走遗留唤醒路径；size 异常值当作缺失（收口判定退化为 terminal==called）。
        tool_batch_id = payload.get("tool_batch_id") or None
        raw_batch_size = payload.get("tool_batch_size")
        tool_batch_size = (
            raw_batch_size
            if isinstance(raw_batch_size, int)
            and not isinstance(raw_batch_size, bool)
            and raw_batch_size > 0
            else None
        )

        scope_key = _scope_key_from_row(scope, group_id, user_id)
        tool = self._registry.get(tool_name)

        def _processed(terminal_event_id: str) -> _ProcessedCall:
            return _ProcessedCall(
                scope_key=scope_key,
                tool_batch_id=tool_batch_id,
                tool_batch_size=tool_batch_size,
                terminal_event_id=terminal_event_id,
                correlation_id=correlation_id,
            )

        if tool is None:
            failed_id = await write_agent_event(
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
            return _processed(failed_id)

        # 出货去重:抢占本条 tool_called 的执行权,再真正跑工具。抢不到(并发实例 /
        # 上次尝试仍在租约内)→ 跳过;事件仍 pending,lease 到期后由延迟 wake
        # 重新扫描,不再依赖后续恰好有新 tool_called 才能复活。
        claim = await claim_delivery(self._session_factory, event_id, "tool")
        if not claim.claimed:
            retry_after = (
                claim.retry_after_seconds
                if claim.retry_after_seconds is not None
                else float(DEFAULT_LEASE_SECONDS)
            )
            logger.info(
                "[tool_worker] tool_called={} already claimed, retry in {:.1f}s",
                event_id,
                retry_after,
            )
            self._schedule_retry(retry_after)
            return None

        claimed_here = True
        terminal_written = False
        try:
            # ── 运行工具，归一成一个 ToolOutcome（纯搬运，不解释业务语义）──
            # BaseTool 工具 run() 永不 raise、直接返回 ToolOutcome（可预期失败也是
            # 返回的失败 outcome）。这里的 try/except 只为兼容非 BaseTool 的裸 stub /
            # 极端情况：拿到 dict 桥接成 success，冒出的预料外异常兜底
            # internal_tool_error（契约 §7.2）。ToolWorker 不 introspect 异常类名。
            try:
                # 系统级 context 统一作为 kwargs 注入；每个工具收到的 context 完全
                # 相同，按需消费、不需要的用 **_ 忽略（黑盒：系统只喂 input、收
                # output，不必按名字特判任何工具）。
                #   scope_key / task_id / correlation_id —— 路由与审计
                #   session_factory                       —— 写/查 agent_events
                #     (search_history / respond_to_group_join_request 等需要)
                #   triggered_by_event_id / triggered_by_user_tier / bot_role
                #     —— 发起人身份 + bot 角色快照，工具内 enforce_access 判权限
                #     （发起人 tier 与 bot 角色都**实时**查 napcat，bot_role 仅作
                #     实时查不到时的回退快照）
                raw = await tool.run(
                    arguments,
                    scope_key=scope_key,
                    task_id=task_id,
                    correlation_id=correlation_id,
                    session_factory=self._session_factory,
                    # 触发用户身份 + bot 角色快照——工具内 enforce_access 实时判
                    # 发起人 tier + bot 自身角色（bot_role 仅作实时查不到的回退）。
                    triggered_by_event_id=triggered_by_event_id,
                    triggered_by_user_tier=triggered_by_user_tier,
                    bot_role=bot_role,
                    # 本条 agent.tool_called 的 event_id——工具若产生后续事件
                    # （如 wait 的 runtime.wait_elapsed）以此作 causation 锚。
                    tool_call_event_id=event_id,
                    # scope 唤醒入口（async callable，签名 wake(scope_key)）——
                    # wait 等"时间自主权"工具用它给模型安排延迟唤醒；supervisor
                    # 未注入（旧测试 / 早期骨架）时为 None，工具自行降级失败。
                    wake_scope=getattr(self._supervisor, "wake", None),
                    # 看图写描述回调（async (bytes, mime, note) -> str）——
                    # meme 工具收录/换描述时生成 description；未接线时为
                    # None，工具自行降级失败。
                    caption_image=self._caption_image,
                    notify_reply_task=getattr(
                        self._supervisor, "notify_reply_task", None
                    ),
                )
            except Exception as exc:
                logger.exception("[tool_worker] {} crashed: {}", tool_name, exc)
                outcome = ToolOutcome.failure(
                    "internal_tool_error", f"{type(exc).__name__}: {exc}"
                )
            else:
                outcome = coerce_tool_outcome(raw)

            # ── 落表：工具声明的领域事件 + terminal 同事务提交 ──
            event_writes = [
                AgentEventWrite(
                    event_type=generated.event_type,
                    causation_id=event_id,
                    payload=generated.payload,
                    occurred_at=generated.occurred_at,
                )
                for generated in outcome.emitted_events
            ]
            if outcome.ok:
                event_writes.append(
                    AgentEventWrite(
                        event_type="agent.tool_result",
                        causation_id=event_id,
                        payload={
                            "tool_call_id": tool_call_id,
                            "tool_name": tool_name,
                            "task_id": task_id,
                            "result": outcome.result,
                        },
                    )
                )
            else:
                logger.warning(
                    "[tool_worker] {} failed: {} {}",
                    tool_name,
                    outcome.error_kind,
                    outcome.error_message,
                )
                fail_payload = {
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_name,
                    "task_id": task_id,
                    "error_kind": outcome.error_kind,
                    "error_message": outcome.error_message,
                }
                if isinstance(outcome.extra, dict):
                    fail_payload.update(outcome.extra)
                event_writes.append(
                    AgentEventWrite(
                        event_type="agent.tool_failed",
                        causation_id=event_id,
                        payload=fail_payload,
                    )
                )
            written_ids = await write_agent_events(
                self._session_factory,
                scope_key=scope_key,
                correlation_id=correlation_id,
                events=event_writes,
            )
            terminal_id = written_ids[-1]
            terminal_written = True
            return _processed(terminal_id)
        finally:
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


def _scope_key_from_row(
    scope: str, group_id: int | None, user_id: int | None
) -> str:
    if scope == "group" and group_id is not None:
        return f"group:{group_id}"
    if scope == "private" and user_id is not None:
        return f"private:{user_id}"
    return "system"

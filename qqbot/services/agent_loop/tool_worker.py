"""ToolWorker — 消费 agent.tool_called 调用 ToolRegistry 并写 agent.tool_result/failed。

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
     supervisor.notify_tool_batch_completed 解闩 + 唤醒该 scope **一次**。
     不再是"每 drain 一轮就按 scope wake"（那会让先完成的工具提前
     唤醒下一拍，慢工具还没回来，模型容易复读）。无批次标记的遗留
     tool_called（升级前落库的）维持旧行为：drain 后按 scope 直接 wake。

幂等：SQL `NOT EXISTS(tool_result|tool_failed WHERE causation_id=tool_called.event_id)`，
重启 / 重入安全；runtime.tool_batch_completed 写前查重（同 batch_id 只写一条），
但已存在时仍会补发解闩通知（修复"写了标记、进程在唤醒前挂了"的半截状态）。

批次判定/completion 事件写入/唤醒时机全在本编排层——工具保持黑盒（输入
arguments、返回 ToolOutcome），对批次一无所知。

执行后**不自动**推进任务状态（pending→running 已由 AgentLoop 在写 tool_called
时附带完成；最终 done/failed 由 LLM 通过 complete_task / fail_task 显式驱动）。

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
    write_agent_event,
    write_runtime_event,
)
from qqbot.services.agent_loop.tool_registry import (
    ToolOutcome,
    ToolRegistry,
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
    ORDER BY r.occurred_at ASC
    LIMIT 100
    """
)

# 批次收口判定：该批次已落库的 tool_called 总数 + 其中已有 terminal
# （tool_result/tool_failed）配对的条数。收口条件 = terminal == called 且
# called >= tool_batch_size —— 后者防住"AgentLoop 还在写同批后续 tool_called、
# drain 恰好撞进写间隙"的竞态（已写的都 terminal 了但整批还没写全）。
_BATCH_STATUS_QUERY = text(
    """
    SELECT
        COUNT(*) AS called,
        COUNT(*) FILTER (
            WHERE EXISTS (
                SELECT 1 FROM agent_events d
                WHERE d.causation_id = r.event_id
                  AND d.type IN ('agent.tool_result', 'agent.tool_failed')
            )
        ) AS terminal,
        COUNT(*) FILTER (
            WHERE r.payload->>'tool_name' <> 'reply'
        ) AS non_reply,
        COUNT(*) FILTER (
            WHERE EXISTS (
                SELECT 1 FROM agent_events d
                WHERE d.causation_id = r.event_id
                  AND d.type = 'agent.tool_failed'
            )
        ) AS failed
    FROM agent_events r
    WHERE r.type = 'agent.tool_called'
      AND r.payload->>'tool_batch_id' = :tool_batch_id
    """
)

_BATCH_COMPLETED_EXISTS_QUERY = text(
    """
    SELECT 1
    FROM agent_events
    WHERE type = 'runtime.tool_batch_completed'
      AND payload->>'tool_batch_id' = :tool_batch_id
    LIMIT 1
    """
)


@dataclass(frozen=True)
class _ProcessedCall:
    """_process_one 写完 terminal 后带回 drain 层的批次线索。"""

    scope_key: str
    tool_batch_id: str | None
    tool_batch_size: int | None
    terminal_event_id: str
    correlation_id: str
    wake_requested: bool = True


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
        batch_should_wake: dict[tuple[str, str], bool] = {}
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
                batch_should_wake[key] = batch_should_wake.get(key, False) or getattr(
                    done, "wake_requested", True
                )
            elif done.wake_requested:
                legacy_scopes.add(done.scope_key)

        # 遗留（无批次标记）：维持旧的"drain 后按 scope 直接唤醒"。若该 scope
        # 恰有新批次门闩开着，AgentLoop 侧会把这次唤醒推迟——不会误开 tick。
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
        # 解闩 + 批次级唤醒一次。判定放在本轮所有 terminal 都落库之后，保证
        # "唤醒到达时完成事件必已在事件流里"。
        for (scope_key, batch_id), last in touched_batches.items():
            try:
                await self._maybe_close_batch(
                    scope_key,
                    batch_id,
                    last,
                    should_wake=batch_should_wake.get((scope_key, batch_id), True),
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

    async def _maybe_close_batch(
        self,
        scope_key: str,
        tool_batch_id: str,
        last: _ProcessedCall,
        *,
        should_wake: bool = True,
    ) -> None:
        """判定批次是否收口；是则写 completion 标记事件并通知 supervisor。"""
        async with self._session_factory() as session:
            result = await session.execute(
                _BATCH_STATUS_QUERY, {"tool_batch_id": tool_batch_id}
            )
            row = result.mappings().first()
        called = int(row["called"] or 0) if row else 0
        terminal = int(row["terminal"] or 0) if row else 0
        if row is not None:
            # “不唤醒”只允许真正的 reply-only 全成功批次。多实例/分轮处理
            # 时，当前 drain 可能只碰到最后一条 reply，必须从整批 DB 事实
            # 恢复是否还含普通工具或失败，不能只看本轮 touched calls。
            non_reply = int(row.get("non_reply") or 0)
            failed = int(row.get("failed") or 0)
            should_wake = should_wake or non_reply > 0 or failed > 0
        if called == 0:
            # 理论不可达（本轮刚处理过该批次的行），防御分支
            return
        if terminal < called:
            return  # 同批还有工具在跑 / 待跑
        expected = last.tool_batch_size
        if expected is not None and called < expected:
            return  # AgentLoop 还没写完同批后续 tool_called（写间隙竞态）

        # 查重：同 batch_id 只写一条 completion；已存在时仍补发解闩通知，
        # 修复"标记写了、进程在唤醒前挂了"的半截状态（通知幂等，多发无害——
        # AgentLoop 侧门闩会吞掉不该开拍的唤醒）。
        async with self._session_factory() as session:
            result = await session.execute(
                _BATCH_COMPLETED_EXISTS_QUERY,
                {"tool_batch_id": tool_batch_id},
            )
            already_written = result.first() is not None
        if not already_written:
            # agent_visible：标记既给调度层当解锁依据，也进模型的 timeline
            # （渲染成 <system-hint kind="tool_batch_completed">）——让模型
            # 显式看到"上一批工具已整体收口"的批次边界，而不是只能从
            # 各 <tool-call> 都 complete 里自己归纳。渲染时剔除 ULID 噪音，
            # 见 projection._render_runtime。
            await write_runtime_event(
                self._session_factory,
                event_type="runtime.tool_batch_completed",
                scope_key=scope_key,
                visibility="agent_visible",
                correlation_id=last.correlation_id,
                causation_id=last.terminal_event_id,
                payload={
                    "tool_batch_id": tool_batch_id,
                    "tool_count": called,
                    "tool_batch_size": expected,
                },
            )
        if self._supervisor is None or not should_wake:
            return
        notify = getattr(
            self._supervisor, "notify_tool_batch_completed", None
        )
        try:
            if notify is not None:
                await notify(scope_key, tool_batch_id)
            else:
                # 旧接口兜底（fake / 早期骨架没有批次门闩）：直接唤醒
                await self._supervisor.wake(scope_key)
            logger.info(
                "[tool_worker] tool batch completed: scope={} batch={} "
                "tools={}",
                scope_key,
                tool_batch_id,
                called,
            )
        except Exception as exc:
            logger.warning(
                "[tool_worker] batch completion notify failed: scope={} "
                "batch={}: {}",
                scope_key,
                tool_batch_id,
                exc,
            )

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

        def _processed(
            terminal_event_id: str, *, wake_requested: bool = True
        ) -> _ProcessedCall:
            return _ProcessedCall(
                scope_key=scope_key,
                tool_batch_id=tool_batch_id,
                tool_batch_size=tool_batch_size,
                terminal_event_id=terminal_event_id,
                correlation_id=correlation_id,
                wake_requested=wake_requested,
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
                outcome = _coerce_outcome(raw)

            # ── 落表：outcome → agent.tool_result | agent.tool_failed ──
            if outcome.ok:
                terminal_id = await write_agent_event(
                    self._session_factory,
                    event_type="agent.tool_result",
                    scope_key=scope_key,
                    correlation_id=correlation_id,
                    causation_id=event_id,
                    payload={
                        "tool_call_id": tool_call_id,
                        "tool_name": tool_name,
                        "task_id": task_id,
                        "result": outcome.result,
                    },
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
                terminal_id = await write_agent_event(
                    self._session_factory,
                    event_type="agent.tool_failed",
                    scope_key=scope_key,
                    correlation_id=correlation_id,
                    causation_id=event_id,
                    payload=fail_payload,
                )
            terminal_written = True
            wake_policy = getattr(tool, "wake_policy", "always")
            if wake_policy == "never":
                wake_requested = False
            elif wake_policy == "on_failure":
                wake_requested = not outcome.ok
            else:
                wake_requested = True
            return _processed(terminal_id, wake_requested=wake_requested)
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


def _coerce_outcome(raw: Any) -> ToolOutcome:
    """把工具返回值归一成 ToolOutcome。

    工具直接返回 ToolOutcome（成功或失败）→ 原样；返回 dict → 桥接成 success
    （兼容轻量 stub）；None → 空 success；其它标量 → 包成 ``{"value": raw}``。
    黑盒工具**永不 raise**：失败由工具**返回** ToolOutcome.failure，这里原样透传。
    """
    if isinstance(raw, ToolOutcome):
        return raw
    if isinstance(raw, dict):
        return ToolOutcome.success(raw)
    if raw is None:
        return ToolOutcome.success({})
    return ToolOutcome.success({"value": raw})


def _scope_key_from_row(
    scope: str, group_id: int | None, user_id: int | None
) -> str:
    if scope == "group" and group_id is not None:
        return f"group:{group_id}"
    if scope == "private" and user_id is not None:
        return f"private:{user_id}"
    return "system"

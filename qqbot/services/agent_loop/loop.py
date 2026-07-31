"""AgentLoop — long-running per-scope decision loop.

One instance per scope_key (group:<id> or system). PrivateAgentLoop is
NOT instantiated in v2 第一版 (实例化策略 §10.1); private events are
ingested but not dispatched here.

Skeleton tick (will grow as projection + real planner come online):
  runtime.tick_started  ──▶  build DecisionContext (stub)
                       └──▶  planner.decide() ─▶ DecisionOutput
                       └──▶  translate actions to agent.* events
                       └──▶  runtime.tick_ended

The loop is awoken by LoopSupervisor.wake(); when idle it parks on an
asyncio.Event without burning CPU.

唤醒攒批窗口（2026-07-28）：wake() 默认**不立刻**开拍，而是把唤醒推迟
_WAKE_DEBOUNCE_SECONDS；窗口内再来的唤醒顺延这个 deadline，直到安静下来才
真正开拍（_WAKE_MAX_DELAY_SECONDS 封顶，防止持续刷屏把 tick 饿死）。
asyncio.Event 本身已经能把"上一拍还在跑"期间的多次唤醒并成一次，但 loop 空闲
时第一条消息会立刻开拍——而 QQ 上一句话拆成三条发是常态，那会让 bot 对着半截
话表态，然后下一拍再看到后半句。窗口堵的是这个洞，不是省 tick。
工具批次收口这类"活干完了，来看结果"的唤醒走 immediate=True 直接开拍：那里
没有什么可攒的，等窗口纯属白白加延迟。

工具批次（tool_batch）：同一 tick 派发的全部 call_tool 属于同一个批次
（tool_batch_id 复用 decision_id），inline/worker 执行方共用收口器，经
supervisor 批次级唤醒一次。2026-07-02 起**没有批次门闩**：批次进行期间
的任何唤醒都随时开拍，
模型自己看 <tool-call status="processing"> 行决定等还是先处理新事件——程序
不替模型决定"何时可以思考"（模型+prompt 优先哲学）。

无效输出重试（任务与决策契约 §7.1）：planner 输出未通过动作校验时，同 tick
内带着 validation_feedback 重试至多 2 次（共 3 次调用），每次失败写一条
runtime.llm_invalid_output（attempt 递增）；三次仍非法才强制
idle(reason="invalid_output_giveup")——先给模型自我修正的机会，而不是一错就没收
本拍的响应权。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from datetime import datetime
from typing import Any, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from qqbot.core.ids import new_event_id
from qqbot.core.logging import get_logger
from qqbot.core.time import china_now
from qqbot.services.agent_loop.decision import (
    Action,
    CallToolAction,
    DecisionContext,
    DecisionOutput,
    IdleAction,
    Planner,
)
from qqbot.services.agent_loop.event_writer import (
    AgentEventWrite,
    write_agent_event,
    write_agent_events,
    write_runtime_event,
)
from qqbot.services.agent_loop.projection import Projector
from qqbot.services.agent_loop.tool_batch import maybe_close_tool_batch
from qqbot.services.agent_loop.tool_registry import (
    Tool,
    ToolOutcome,
    ToolRegistry,
    coerce_tool_outcome,
    get_tool_execution_mode,
)

logger = get_logger(__name__)

# 唤醒攒批窗口（2026-07-28，见模块 docstring）。安静 _WAKE_DEBOUNCE_SECONDS
# 后才开拍；一串连续唤醒最多把首次唤醒推迟 _WAKE_MAX_DELAY_SECONDS —— 没有这
# 个上限的话，一个持续刷屏的群会把 deadline 无限往后推，loop 永远不开拍。
_WAKE_DEBOUNCE_SECONDS = 2.0
_WAKE_MAX_DELAY_SECONDS = 6.0

SessionFactory = Callable[[], AsyncSession]


class AgentLoop:
    def __init__(
        self,
        scope_key: str,
        planner: Planner,
        session_factory: SessionFactory,
        projector: Projector | None = None,
        supervisor: Any | None = None,
        bot_user_id_resolver: Callable[[], str | None] | None = None,
        tool_registry: ToolRegistry | None = None,
        caption_image: Any | None = None,
    ) -> None:
        self._scope_key = scope_key
        self._planner = planner
        self._session_factory = session_factory
        self._projector = projector
        # supervisor 鸭子类型注入，规避 supervisor → loop 的循环 import。
        # 用到 notify_tool_pending()（worker 调用落库后叫醒 ToolWorker）以及
        # notify_tool_batch_completed()（inline 批次收口通知）。批次门闩接口
        # 已于 2026-07-02 随门闩拆除。
        self._supervisor = supervisor
        # bot_user_id 每 tick 重新 resolve —— bot 重连后 self_id 不变但实例
        # 会换；启动初期可能返回 None，prompt 渲染层接受 None 优雅降级。
        # None resolver 表示不注入（旧测试 / 早期骨架兼容）。
        self._bot_user_id_resolver = bot_user_id_resolver
        # registry 只参与通用 execution_mode 路由；权限/scope/role 判定仍全部
        # 下放工具内 BaseTool.enforce_access。缺 registry 时任何调用都按 worker
        # 模式派发，保持旧测试 / 早期骨架兼容。
        self._tool_registry = tool_registry
        # 与 ToolWorker 相同的可选工具依赖；inline 工具也收到完整 context。
        self._caption_image = caption_image
        self._wake = asyncio.Event()
        self._stopped = False
        self._tick_seq = 0
        self._task: asyncio.Task[None] | None = None
        # 攒批窗口状态：deadline 每次 wake() 顺延，burst_started 钉住这一串
        # 唤醒的起点（用于 _WAKE_MAX_DELAY_SECONDS 封顶）；timer 是当前在睡的
        # 那个协程，同一时刻至多一个。
        self._wake_deadline: float | None = None
        self._wake_burst_started: float | None = None
        self._wake_timer: asyncio.Task[None] | None = None

    @property
    def scope_key(self) -> str:
        return self._scope_key

    def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(
            self._run(), name=f"agent_loop:{self._scope_key}"
        )

    async def stop(self) -> None:
        self._stopped = True
        self._cancel_wake_timer()
        self._wake.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            finally:
                self._task = None

    def wake(self, *, immediate: bool = False) -> None:
        """请求开拍。默认走攒批窗口（见模块 docstring）。

        immediate=True 用于"活干完了，来看结果"类唤醒（工具批次收口）：那里
        没有后续消息可攒，等窗口只是白加延迟。
        """
        if self._stopped:
            return
        if immediate or _WAKE_DEBOUNCE_SECONDS <= 0:
            self._cancel_wake_timer()
            self._wake_deadline = None
            self._wake_burst_started = None
            self._wake.set()
            return

        now = time.monotonic()
        if self._wake_burst_started is None:
            self._wake_burst_started = now
        # 新唤醒把 deadline 往后推，但不越过这一串唤醒的硬上限。
        self._wake_deadline = min(
            now + _WAKE_DEBOUNCE_SECONDS,
            self._wake_burst_started + _WAKE_MAX_DELAY_SECONDS,
        )
        if self._wake_timer is None or self._wake_timer.done():
            self._wake_timer = asyncio.create_task(
                self._wake_after_quiet(),
                name=f"agent_loop_wake:{self._scope_key}",
            )

    async def _wake_after_quiet(self) -> None:
        """睡到 deadline 再置位。窗口内又有新唤醒 → wake() 已把
        _wake_deadline 推后，这里重读后接着睡（不新建 timer）。"""
        while not self._stopped:
            deadline = self._wake_deadline
            if deadline is None:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(remaining)
        self._wake_deadline = None
        self._wake_burst_started = None
        self._wake.set()

    def _cancel_wake_timer(self) -> None:
        timer, self._wake_timer = self._wake_timer, None
        if timer is not None and not timer.done():
            timer.cancel()

    async def _run(self) -> None:
        logger.info("[loop {}] started", self._scope_key)
        try:
            while not self._stopped:
                await self._wake.wait()
                self._wake.clear()
                if self._stopped:
                    break
                # 2026-07-02 起任何唤醒都直接开拍（批次门闩已拆除）：上一拍
                # 工具还在跑时醒来，投影里对应 <tool-call status="processing">
                # 行，模型自己决定等批次还是先处理新事件。
                try:
                    await self._tick()
                except Exception as exc:
                    logger.exception(
                        "[loop {}] tick failed: {}", self._scope_key, exc
                    )
        finally:
            logger.info("[loop {}] stopped", self._scope_key)

    async def _tick(self) -> None:
        self._tick_seq += 1
        correlation_id = new_event_id()
        now = china_now()

        # runtime.tick_started
        tick_started_id = await write_runtime_event(
            self._session_factory,
            event_type="runtime.tick_started",
            scope_key=self._scope_key,
            visibility="runtime_only",
            correlation_id=correlation_id,
            causation_id=None,
            payload={"tick_seq": self._tick_seq},
        )

        # bot_user_id resolve 失败不应让整 tick 翻车：捕一下、降为 None
        # 走"老行为"（prompt 不渲染 bot_user_id 属性）。
        bot_user_id: str | None = None
        if self._bot_user_id_resolver is not None:
            try:
                resolved = self._bot_user_id_resolver()
                if resolved is not None:
                    bot_user_id = str(resolved)
            except Exception as exc:
                logger.warning(
                    "[loop {}] bot_user_id_resolver failed: {}",
                    self._scope_key,
                    exc,
                )

        # Projector 可选注入：未注入时回退为空 context（早期骨架兼容）
        if self._projector is not None:
            try:
                context = await self._projector.build_context(
                    scope_key=self._scope_key,
                    correlation_id=correlation_id,
                    tick_seq=self._tick_seq,
                    now=now,
                    bot_user_id=bot_user_id,
                )
            except Exception as exc:
                logger.exception(
                    "[loop {}] projection failed, falling back to empty context: {}",
                    self._scope_key,
                    exc,
                )
                context = DecisionContext(
                    scope_key=self._scope_key,
                    correlation_id=correlation_id,
                    tick_seq=self._tick_seq,
                    now=now,
                    bot_user_id=bot_user_id,
                )
        else:
            context = DecisionContext(
                scope_key=self._scope_key,
                correlation_id=correlation_id,
                tick_seq=self._tick_seq,
                now=now,
                bot_user_id=bot_user_id,
            )

        # ─── 决策 + 校验重试（任务与决策契约 §7.1）───
        # 输出非法不没收本拍：带着 validation_feedback 同 tick 重试至多 2 次
        # （共 3 次调用），把改错的机会还给模型；每次失败写一条
        # runtime.llm_invalid_output（attempt 递增，agent_visible——即便本拍
        # 修好了，下一拍模型也能看到自己犯过错）。三次仍非法才强制 idle。
        decision: DecisionOutput | None = None
        validation_error: str | None = None
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            attempt_context = (
                context
                if validation_error is None
                else replace(
                    context,
                    validation_feedback=(
                        f"attempt {attempt - 1} rejected: {validation_error}"
                    ),
                )
            )
            try:
                decision = await self._planner.decide(attempt_context)
            except Exception as exc:
                logger.exception(
                    "[loop {}] planner failed: {}", self._scope_key, exc
                )
                await self._write_tick_ended(
                    correlation_id, tick_started_id, actions_count=0
                )
                return
            validation_error = _validate_decision(decision)
            if validation_error is None:
                break
            await write_runtime_event(
                self._session_factory,
                event_type="runtime.llm_invalid_output",
                scope_key=self._scope_key,
                visibility="agent_visible",
                correlation_id=correlation_id,
                causation_id=None,
                payload={
                    "validation_error": validation_error,
                    "attempt": attempt,
                },
            )
        if validation_error is not None or decision is None:
            decision = DecisionOutput(
                actions=[IdleAction(reason="invalid_output_giveup")],
                reasoning=(
                    f"auto-forced after {max_attempts} invalid attempts: "
                    f"{validation_error}"
                ),
            )

        # agent.decision_emitted
        # occurred_at 显式回填为**本拍投影时刻**（tick 开头的 now），不取默认
        # 的写入时刻（2026-07-24，待办#18）：投影读于 planner.decide() 之前，
        # 事件却写于 LLM 返回之后，而事件流按 occurred_at 排序（_fetch）。用
        # 写入时刻会把 LLM 往返期间到达的消息排到决策事件**之前**——那些消息
        # 根本没进本拍 context，却因此被读成"这拍已经看过"，人连发的第二句就
        # 此被吞；<my-thought> 行同样会渲染到它们之后，位置信号跟着一起错。
        # 决策"发生"于开始思考的时刻，回填后 timeline 的先后关系才与"这拍看到
        # 了什么"一致。
        decision_id = await write_agent_event(
            self._session_factory,
            event_type="agent.decision_emitted",
            scope_key=self._scope_key,
            correlation_id=correlation_id,
            causation_id=None,
            payload={
                "reasoning": decision.reasoning,
                "actions": [{"type": a.type} for a in decision.actions],
                "tick_seq": self._tick_seq,
            },
            occurred_at=now,
        )

        await self._apply_actions(
            decision.actions, correlation_id, decision_id, context
        )

        await self._write_tick_ended(
            correlation_id, tick_started_id, actions_count=len(decision.actions)
        )

    async def _apply_actions(
        self,
        actions: list[Action],
        correlation_id: str,
        decision_id: str,
        context: DecisionContext,
    ) -> None:
        """Translate every action into agent.* events.

        Planner action 只剩 idle / call_tool。task 是 execution_mode="inline"
        的普通工具：当前 tick 内 await，结果里的 task_ref → task_id 映射可供
        后续 CallToolAction 复用；系统不按工具名写任何特判。

        权限：loop **不做任何业务权限/scope/role 判定，也不解析触发用户 tier**——
        只把"谁触发"的 anchor（triggered_by_event_id）与已折好的 bot 角色
        （context.bot_role）原样写进 ``agent.tool_called.payload`` 交给工具。scope、
        发起人 tier（工具内**实时**查群角色）、bot 自身角色的判定全部下放到工具内
        （BaseTool.enforce_access = enforce_scope + enforce_permission +
        enforce_bot_admin），失败由工具返回语义化 error_kind（见 §2.2、§7.2）。

        """
        ref_to_task_id: dict[str, str] = {}

        # 同拍动作事件的 occurred_at 一律回填为本拍投影时刻（2026-07-27，补齐
        # 待办#18 的另一半）：这些事件与 decision_emitted 同属快照时刻拍板的
        # 决策产物，取默认写入时刻会让 LLM 往返期间到达的消息排到
        # <tool-call> 行之前——下一拍会把没看过的消息读成"落稿前已看过、
        # 有意不接"。同拍各事件时间戳因此相同，相对先后由 event_id
        # （ULID 单调）承载。
        now = context.now

        # ─── 工具批次（tool_batch）───
        # 同一 tick 派发的全部 call_tool 属于同一批次：tool_batch_id 直接复用
        # decision_id（同拍唯一即可，不另造 ID 体系），tool_batch_size = 本
        # actions 里 call_tool 的个数。共享收口器据 (id, size) 判定"整批全部
        # terminal"后写 runtime.tool_batch_completed 并批次级唤醒一次。批次
        # 只是"结果聚合 + 单次唤醒"的效率单位——没有门闩，期间任何唤醒随时开拍。
        tool_batch_size = sum(
            1 for a in actions if isinstance(a, CallToolAction)
        )

        for action in actions:
            if isinstance(action, IdleAction):
                await write_agent_event(
                    self._session_factory,
                    event_type="agent.idle_decision",
                    scope_key=self._scope_key,
                    correlation_id=correlation_id,
                    causation_id=decision_id,
                    payload={"reason": action.reason},
                    occurred_at=now,
                )
                continue
            if isinstance(action, CallToolAction):
                await self._dispatch_tool_call(
                    action,
                    correlation_id=correlation_id,
                    decision_id=decision_id,
                    context=context,
                    occurred_at=now,
                    tool_batch_size=tool_batch_size,
                    ref_to_task_id=ref_to_task_id,
                )
                continue
            logger.warning(
                "[loop {}] unknown action type: {}",
                self._scope_key,
                type(action).__name__,
            )

    async def _dispatch_tool_call(  # noqa: PLR0913
        self,
        action: CallToolAction,
        *,
        correlation_id: str,
        decision_id: str,
        context: DecisionContext,
        occurred_at: datetime,
        tool_batch_size: int,
        ref_to_task_id: dict[str, str],
    ) -> None:
        """按工具声明的 execution_mode 选择当前 tick 或 ToolWorker 执行。"""
        tool_call_id = new_event_id()
        called_event_id = new_event_id()
        task_id = action.task_id or (
            ref_to_task_id.get(action.task_ref) if action.task_ref else None
        )
        triggered_event_id = action.triggered_by_event_id
        if triggered_event_id is None and task_id is not None:
            triggered_event_id = _find_task_anchor(context, task_id)

        called_payload = {
            "tool_call_id": tool_call_id,
            "tool_name": action.tool_name,
            "arguments": action.arguments,
            "task_id": task_id,
            "triggered_by_event_id": triggered_event_id,
            "bot_role": context.bot_role,
            "tool_batch_id": decision_id,
            "tool_batch_size": tool_batch_size,
        }
        writes = [
            AgentEventWrite(
                event_type="agent.tool_called",
                causation_id=decision_id,
                payload=called_payload,
                occurred_at=occurred_at,
                event_id=called_event_id,
            )
        ]
        if task_id is not None:
            writes.append(
                AgentEventWrite(
                    event_type="agent.task_state_changed",
                    causation_id=called_event_id,
                    payload={
                        "task_id": task_id,
                        "from_state": "pending",
                        "to_state": "running",
                        "reason": None,
                    },
                    occurred_at=occurred_at,
                )
            )

        tool = (
            self._tool_registry.get(action.tool_name)
            if self._tool_registry is not None
            else None
        )
        if tool is None or get_tool_execution_mode(tool) == "worker":
            await write_agent_events(
                self._session_factory,
                scope_key=self._scope_key,
                correlation_id=correlation_id,
                events=writes,
            )
            self._notify_tool_worker()
            return

        outcome = await self._run_inline_tool(
            tool,
            action.arguments,
            task_id=task_id,
            correlation_id=correlation_id,
            triggered_by_event_id=triggered_event_id,
            bot_role=context.bot_role,
            tool_call_event_id=called_event_id,
        )
        writes.extend(
            AgentEventWrite(
                event_type=generated.event_type,
                causation_id=called_event_id,
                payload=generated.payload,
                occurred_at=generated.occurred_at or occurred_at,
            )
            for generated in outcome.emitted_events
        )
        writes.append(
            _terminal_event_write(
                outcome,
                tool_call_id=tool_call_id,
                tool_name=action.tool_name,
                task_id=task_id,
                called_event_id=called_event_id,
            )
        )
        written_ids = await write_agent_events(
            self._session_factory,
            scope_key=self._scope_key,
            correlation_id=correlation_id,
            events=writes,
        )
        _remember_task_ref(outcome, ref_to_task_id)
        try:
            await maybe_close_tool_batch(
                self._session_factory,
                supervisor=self._supervisor,
                scope_key=self._scope_key,
                tool_batch_id=decision_id,
                tool_batch_size=tool_batch_size,
                terminal_event_id=written_ids[-1],
                correlation_id=correlation_id,
            )
        except Exception as exc:
            logger.exception(
                "[loop {}] inline batch close failed: batch={}: {}",
                self._scope_key,
                decision_id,
                exc,
            )

    async def _run_inline_tool(  # noqa: PLR0913
        self,
        tool: Tool,
        arguments: dict,
        *,
        task_id: str | None,
        correlation_id: str,
        triggered_by_event_id: str | None,
        bot_role: str | None,
        tool_call_event_id: str,
    ) -> ToolOutcome:
        """在当前 tick await 工具；裸 stub 抛错也收敛成失败 outcome。"""
        try:
            raw = await tool.run(
                arguments,
                scope_key=self._scope_key,
                task_id=task_id,
                correlation_id=correlation_id,
                session_factory=self._session_factory,
                triggered_by_event_id=triggered_by_event_id,
                triggered_by_user_tier=None,
                bot_role=bot_role,
                tool_call_event_id=tool_call_event_id,
                wake_scope=getattr(self._supervisor, "wake", None),
                caption_image=self._caption_image,
                notify_reply_task=getattr(
                    self._supervisor, "notify_reply_task", None
                ),
            )
        except Exception as exc:
            logger.exception(
                "[loop {}] inline tool {} crashed: {}",
                self._scope_key,
                getattr(tool, "name", "?"),
                exc,
            )
            return ToolOutcome.failure(
                "internal_tool_error",
                f"{type(exc).__name__}: {exc}",
            )
        return coerce_tool_outcome(raw)

    def _notify_tool_worker(self) -> None:
        if self._supervisor is None:
            return
        try:
            self._supervisor.notify_tool_pending()
        except Exception as exc:
            logger.warning(
                "[loop {}] notify_tool_pending failed: {}",
                self._scope_key,
                exc,
            )

    async def _write_tick_ended(
        self,
        correlation_id: str,
        tick_started_id: str,
        actions_count: int,
    ) -> None:
        await write_runtime_event(
            self._session_factory,
            event_type="runtime.tick_ended",
            scope_key=self._scope_key,
            visibility="runtime_only",
            correlation_id=correlation_id,
            causation_id=tick_started_id,
            payload={
                "tick_seq": self._tick_seq,
                "actions_count": actions_count,
            },
        )


def _terminal_event_write(
    outcome: ToolOutcome,
    *,
    tool_call_id: str,
    tool_name: str,
    task_id: str | None,
    called_event_id: str,
) -> AgentEventWrite:
    if outcome.ok:
        return AgentEventWrite(
            event_type="agent.tool_result",
            causation_id=called_event_id,
            payload={
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "task_id": task_id,
                "result": outcome.result,
            },
        )
    payload = {
        "tool_call_id": tool_call_id,
        "tool_name": tool_name,
        "task_id": task_id,
        "error_kind": outcome.error_kind,
        "error_message": outcome.error_message,
    }
    if isinstance(outcome.extra, dict):
        payload.update(outcome.extra)
    return AgentEventWrite(
        event_type="agent.tool_failed",
        causation_id=called_event_id,
        payload=payload,
    )


def _remember_task_ref(
    outcome: ToolOutcome,
    ref_to_task_id: dict[str, str],
) -> None:
    """从任意 inline 成功结果学习同拍 task_ref，不按工具名特判。"""
    if not outcome.ok or not isinstance(outcome.result, dict):
        return
    task_id = outcome.result.get("task_id")
    task_ref = outcome.result.get("task_ref")
    if (
        isinstance(task_id, str)
        and task_id
        and isinstance(task_ref, str)
        and task_ref
    ):
        ref_to_task_id[task_ref] = task_id


def _find_task_anchor(
    context: DecisionContext, task_id: str
) -> str | None:
    """从 DecisionContext.active_tasks 里取 task 的 triggered_by_event_id。

    敏感工具调用没填 triggered_by_event_id 时，AgentLoop fall back 到
    "调用挂的 task 是哪条消息触发的" 补全因果链 —— 这与 task(create) 的 anchor
    语义一致：task 是"我要给小王查天气"，那 task 内任何敏感操作都视作小王的指
    令。task 不存在或没填 anchor 时返回 None（工具内 enforce_permission 据此把
    发起人当 GUEST，敏感工具自然失败）。
    """
    for t in context.active_tasks:
        if t.task_id == task_id:
            return t.triggered_by_event_id
    return None


def _validate_decision(decision: DecisionOutput) -> str | None:
    """Return a short error string on invalid output, or None if valid.

    Rules (任务与决策契约 §3.1, §3.2.3):
    - IdleAction never co-exists with another action.

    工具自己的 arguments / scope / permission 校验全部留在工具边界；这里仅
    校验跨 action 的组合约束。"一 tick 多回复"的旧硬约束已经移除，由
    group_chat_rules.md 软规范引导。
    """
    actions = decision.actions
    if any(isinstance(a, IdleAction) for a in actions) and len(actions) > 1:
        return "idle_with_other_actions"
    return None

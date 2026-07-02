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

工具批次（tool_batch）：同一 tick 派发的全部 call_tool 属于同一个批次
（tool_batch_id 复用 decision_id），批次收口时 ToolWorker 经 supervisor 批次级
唤醒一次。2026-07-02 起**没有批次门闩**：批次进行期间的任何唤醒都随时开拍，
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
from dataclasses import replace
from typing import Any, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from qqbot.core.ids import new_event_id
from qqbot.core.logging import get_logger
from qqbot.core.time import china_now
from qqbot.services.agent_loop.decision import (
    Action,
    CallToolAction,
    CompleteTaskAction,
    CreateTaskAction,
    DecisionContext,
    DecisionOutput,
    FailTaskAction,
    IdleAction,
    NoteTaskProgressAction,
    Planner,
)
from qqbot.services.agent_loop.event_writer import (
    write_agent_event,
    write_runtime_event,
)
from qqbot.services.agent_loop.projection import Projector
from qqbot.services.agent_loop.tool_registry import ToolRegistry

logger = get_logger(__name__)

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
    ) -> None:
        self._scope_key = scope_key
        self._planner = planner
        self._session_factory = session_factory
        self._projector = projector
        # supervisor 鸭子类型注入，规避 supervisor → loop 的循环 import。
        # 唯一用到的接口：notify_tool_pending()（写完 tool_called 后叫醒
        # ToolWorker 立即执行）。批次门闩相关接口已于 2026-07-02 随门闩拆除。
        self._supervisor = supervisor
        # bot_user_id 每 tick 重新 resolve —— bot 重连后 self_id 不变但实例
        # 会换；启动初期可能返回 None，prompt 渲染层接受 None 优雅降级。
        # None resolver 表示不注入（旧测试 / 早期骨架兼容）。
        self._bot_user_id_resolver = bot_user_id_resolver
        # tool_registry 目前仅作为句柄保留：scope / 发起人 tier / bot 角色的
        # **判定与解析**已全部下放到工具内 BaseTool.enforce_access，dispatch 路径
        # 不再查它的元数据、不做任何权限/scope 闸门（catalog 可见性过滤在 planner
        # 组装 prompt 时按 scope 做）。注入 None 亦可——旧测试 / 早期骨架兼容。
        self._tool_registry = tool_registry
        self._wake = asyncio.Event()
        self._stopped = False
        self._tick_seq = 0
        self._task: asyncio.Task[None] | None = None

    @property
    def scope_key(self) -> str:
        return self._scope_key

    def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name=f"agent_loop:{self._scope_key}")

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

    def wake(self) -> None:
        if self._stopped:
            return
        self._wake.set()

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
            validation_error = _validate_decision(
                decision, scope_key=self._scope_key
            )
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

        Maintains an in-tick task_ref → task_id map so a CallToolAction can
        attach to a task created in the same actions list.

        权限：loop **不做任何业务权限/scope/role 判定，也不解析触发用户 tier**——
        只把"谁触发"的 anchor（triggered_by_event_id）与已折好的 bot 角色
        （context.bot_role）原样写进 ``agent.tool_called.payload`` 交给工具。scope、
        发起人 tier（工具内**实时**查群角色）、bot 自身角色的判定全部下放到工具内
        （BaseTool.enforce_access = enforce_scope + enforce_permission +
        enforce_bot_admin），失败由工具返回语义化 error_kind（见 §2.2、§7.2）。

        在循环开始处 lazy 加载一次 SUPERUSERS（reading env file per-tick is
        cheap but per-action is wasteful），同 tick 内复用。
        """
        ref_to_task_id: dict[str, str] = {}

        # ─── 工具批次（tool_batch）───
        # 同一 tick 派发的全部 call_tool 属于同一批次：tool_batch_id 直接复用
        # decision_id（同拍唯一即可，不另造 ID 体系），tool_batch_size = 本
        # actions 里 call_tool 的个数。ToolWorker 据 (id, size) 判定"整批全部
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
                )

            elif isinstance(action, CreateTaskAction):
                task_id = new_event_id()
                await write_agent_event(
                    self._session_factory,
                    event_type="agent.task_created",
                    scope_key=self._scope_key,
                    correlation_id=correlation_id,
                    causation_id=decision_id,
                    payload={
                        "task_id": task_id,
                        "description": action.description,
                        "related_tools": action.related_tools,
                        "parent_task_id": action.parent_task_id,
                        "triggered_by_event_id": action.triggered_by_event_id,
                    },
                )
                if action.task_ref:
                    ref_to_task_id[action.task_ref] = task_id

            elif isinstance(action, CallToolAction):
                tool_call_id = new_event_id()
                task_id = action.task_id or (
                    ref_to_task_id.get(action.task_ref)
                    if action.task_ref
                    else None
                )
                # ─── 注入触发身份（不做任何 role/tier/scope 判定）───
                # AgentLoop 把工具调用所需的"谁触发"线索原样注入，**不解析 tier、
                # 不查角色、不拦 scope**：scope 由工具 enforce_scope 自判、发起人
                # tier 与 bot 角色由工具 enforce_access 现场解析 + 自判（契约 §2.2、
                # §7.2）。这里只确定 triggered_by_event_id（LLM 给的；缺则回退到 task
                # 的 anchor 补全因果链），其余交给工具。
                triggered_event_id = action.triggered_by_event_id
                if triggered_event_id is None and task_id is not None:
                    triggered_event_id = _find_task_anchor(context, task_id)
                # ─── dispatch ───
                called_event_id = await write_agent_event(
                    self._session_factory,
                    event_type="agent.tool_called",
                    scope_key=self._scope_key,
                    correlation_id=correlation_id,
                    causation_id=decision_id,
                    payload={
                        "tool_call_id": tool_call_id,
                        "tool_name": action.tool_name,
                        "arguments": action.arguments,
                        "task_id": task_id,
                        # 注入工具自判所需的上下文：谁触发（工具据此现场解析发起人
                        # tier）+ bot 自身角色。loop 自己不解析 tier、不写
                        # triggered_by_user_id / triggered_by_user_tier。
                        "triggered_by_event_id": triggered_event_id,
                        "bot_role": context.bot_role,
                        # 批次标记：ToolWorker 据此判定"同拍整批是否全部 terminal"
                        # ——批次边界是编排层（loop/worker/supervisor）的职责，
                        # 工具本身对批次一无所知（黑盒不变）。
                        "tool_batch_id": decision_id,
                        "tool_batch_size": tool_batch_size,
                    },
                )
                # 叫醒 ToolWorker 立即执行（同 send_message 那条线的推+拉策略）
                if self._supervisor is not None:
                    try:
                        self._supervisor.notify_tool_pending()
                    except Exception as exc:
                        logger.warning(
                            "[loop {}] notify_tool_pending failed: {}",
                            self._scope_key,
                            exc,
                        )
                # Auto-advance task state pending → running on first tool_called
                # (任务与决策契约 §4.1). The projection layer will skip the
                # transition if the task is already running.
                if task_id is not None:
                    await write_agent_event(
                        self._session_factory,
                        event_type="agent.task_state_changed",
                        scope_key=self._scope_key,
                        correlation_id=correlation_id,
                        causation_id=called_event_id,
                        payload={
                            "task_id": task_id,
                            "from_state": "pending",
                            "to_state": "running",
                            "reason": None,
                        },
                    )

            elif isinstance(action, CompleteTaskAction):
                await write_agent_event(
                    self._session_factory,
                    event_type="agent.task_state_changed",
                    scope_key=self._scope_key,
                    correlation_id=correlation_id,
                    causation_id=decision_id,
                    payload={
                        "task_id": action.task_id,
                        "to_state": "done",
                        "reason": action.result_summary,
                    },
                )

            elif isinstance(action, FailTaskAction):
                await write_agent_event(
                    self._session_factory,
                    event_type="agent.task_state_changed",
                    scope_key=self._scope_key,
                    correlation_id=correlation_id,
                    causation_id=decision_id,
                    payload={
                        "task_id": action.task_id,
                        "to_state": "failed",
                        "reason": action.reason,
                    },
                )

            elif isinstance(action, NoteTaskProgressAction):
                # 进度笔记 — 不改 state，仅落事件供下一 tick 的 fold_tasks
                # 取到尾部 N 条渲染进 TaskView.progress_notes。
                await write_agent_event(
                    self._session_factory,
                    event_type="agent.task_progress_noted",
                    scope_key=self._scope_key,
                    correlation_id=correlation_id,
                    causation_id=decision_id,
                    payload={
                        "task_id": action.task_id,
                        "note": action.note,
                    },
                )

            else:
                logger.warning(
                    "[loop {}] unknown action type: {}",
                    self._scope_key,
                    type(action).__name__,
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


def _find_task_anchor(
    context: DecisionContext, task_id: str
) -> str | None:
    """从 DecisionContext.active_tasks 里取 task 的 triggered_by_event_id。

    敏感工具调用没填 triggered_by_event_id 时，AgentLoop fall back 到
    "调用挂的 task 是哪条消息触发的" 补全因果链 —— 这与 create_task 的 anchor
    语义一致：task 是"我要给小王查天气"，那 task 内任何敏感操作都视作小王的指
    令。task 不存在或没填 anchor 时返回 None（工具内 enforce_permission 据此把
    发起人当 GUEST，敏感工具自然失败）。
    """
    for t in context.active_tasks:
        if t.task_id == task_id:
            return t.triggered_by_event_id
    return None


def _validate_decision(decision: DecisionOutput, *, scope_key: str) -> str | None:
    """Return a short error string on invalid output, or None if valid.

    Rules (任务与决策契约 §3.1, §3.2.3):
    - IdleAction never co-exists with another action.

    Reply 校验下沉到 SendMessageTool.run() —— 由于发言现在是普通工具，
    target.kind/group_id 与 scope_key 不匹配时 tool 自身返回失败 outcome，
    ToolWorker 写 agent.tool_failed；"一 tick 多回复"的硬约束移除，
    由 group_chat_rules.md 软规范引导（多发短消息有时是合理选择）。
    """
    actions = decision.actions
    if any(isinstance(a, IdleAction) for a in actions) and len(actions) > 1:
        return "idle_with_other_actions"
    return None

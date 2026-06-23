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
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from qqbot.core.ids import new_event_id
from qqbot.core.logging import get_logger
from qqbot.core.permissions import (
    PermissionTier,
    load_superusers,
    resolve_user_tier_from_event,
)
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
from qqbot.services.agent_loop.tool_registry import (
    ToolRegistry,
    get_tool_require_bot_admin,
    get_tool_required_permission,
)

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
        # 用到的接口仅 notify_reply_pending()（reply 触发即时投递）。
        self._supervisor = supervisor
        # bot_user_id 每 tick 重新 resolve —— bot 重连后 self_id 不变但实例
        # 会换；启动初期可能返回 None，prompt 渲染层接受 None 优雅降级。
        # None resolver 表示不注入（旧测试 / 早期骨架兼容）。
        self._bot_user_id_resolver = bot_user_id_resolver
        # tool_registry 注入是为了 dispatch CallToolAction 之前查工具的权限
        # 元数据（required_permission / require_bot_admin）并实施闸门。注入
        # None 时闸门退化为不检查 —— 旧测试 / 早期骨架兼容。生产路径
        # supervisor 总会注入。
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

        try:
            decision: DecisionOutput = await self._planner.decide(context)
        except Exception as exc:
            logger.exception(
                "[loop {}] planner failed: {}", self._scope_key, exc
            )
            await self._write_tick_ended(
                correlation_id, tick_started_id, actions_count=0
            )
            return

        # Validate the actions list; on failure write runtime.llm_invalid_output
        # and force idle for this tick (任务与决策契约 §7.1).
        validation_error = _validate_decision(decision, scope_key=self._scope_key)
        if validation_error is not None:
            await write_runtime_event(
                self._session_factory,
                event_type="runtime.llm_invalid_output",
                scope_key=self._scope_key,
                visibility="agent_visible",
                correlation_id=correlation_id,
                causation_id=None,
                payload={"validation_error": validation_error, "attempt": 1},
            )
            decision = DecisionOutput(
                actions=[IdleAction(reason=f"invalid_output:{validation_error}")],
                reasoning="auto-forced after validation failure",
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

        权限闸门：对 CallToolAction 在写 ``agent.tool_called`` 之前做两层校验
        （触发用户 tier vs tool.required_permission；bot 角色 vs
        tool.require_bot_admin），不通过直接写 ``agent.tool_failed`` 不进
        ToolWorker 队列。所有校验素材都来自 ``context``（bot_role）或现查
        DB（triggered_by_event_id 解析的 sender.role），见 §4。

        在循环开始处 lazy 加载一次 SUPERUSERS（reading env file per-tick is
        cheap but per-action is wasteful），同 tick 内复用。
        """
        ref_to_task_id: dict[str, str] = {}
        # 同一 tick 多次 dispatch 共享一份 SUPERUSERS 快照，避免每个 action
        # 重读 env。tier 解析时下沉传入。
        _superusers_cache: frozenset[str] | None = None

        async def _resolve_tier(
            event_id: str | None,
        ) -> tuple[PermissionTier, str | None]:
            nonlocal _superusers_cache
            if _superusers_cache is None:
                _superusers_cache = load_superusers()
            return await resolve_user_tier_from_event(
                event_id,
                session_factory=self._session_factory,
                superusers=_superusers_cache,
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
                # ─── 权限闸门 ───
                # 1) 找到工具元数据（tool_registry 注入了才校验；老测试 /
                #    早期骨架场景下没注入，退化为只 audit）。
                tool_obj = (
                    self._tool_registry.get(action.tool_name)
                    if self._tool_registry is not None
                    else None
                )
                required_tier = (
                    get_tool_required_permission(tool_obj)
                    if tool_obj is not None
                    else PermissionTier.GUEST
                )
                require_bot_admin = (
                    get_tool_require_bot_admin(tool_obj)
                    if tool_obj is not None
                    else False
                )
                # 2) bot 角色检查：tick-级常量，先判（不需要 DB）
                if require_bot_admin and context.bot_role not in ("admin", "owner"):
                    await write_agent_event(
                        self._session_factory,
                        event_type="agent.tool_failed",
                        scope_key=self._scope_key,
                        correlation_id=correlation_id,
                        causation_id=decision_id,
                        payload={
                            "tool_call_id": tool_call_id,
                            "tool_name": action.tool_name,
                            "task_id": task_id,
                            "error_kind": "permission_denied_bot_role",
                            "error_message": (
                                f"this tool requires the bot itself to be admin/owner "
                                f"in this group; current bot_role="
                                f"{context.bot_role or 'unknown'}"
                            ),
                            "required_bot_role": "admin",
                            "actual_bot_role": context.bot_role,
                            "triggered_by_event_id": action.triggered_by_event_id,
                        },
                    )
                    continue
                # 3) 触发用户 tier 检查：仅敏感工具才查 DB
                triggered_event_id = action.triggered_by_event_id
                triggered_user_id: str | None = None
                user_tier = PermissionTier.GUEST
                if required_tier > PermissionTier.GUEST:
                    # 若 LLM 在 action 上没填，再 fall back 到 task 的 anchor
                    if triggered_event_id is None and task_id is not None:
                        triggered_event_id = _find_task_anchor(
                            context, task_id
                        )
                    user_tier, triggered_user_id = await _resolve_tier(
                        triggered_event_id
                    )
                    if user_tier < required_tier:
                        await write_agent_event(
                            self._session_factory,
                            event_type="agent.tool_failed",
                            scope_key=self._scope_key,
                            correlation_id=correlation_id,
                            causation_id=decision_id,
                            payload={
                                "tool_call_id": tool_call_id,
                                "tool_name": action.tool_name,
                                "task_id": task_id,
                                "error_kind": "permission_denied_user_tier",
                                "error_message": (
                                    f"triggering user tier {user_tier.name} is below "
                                    f"required {required_tier.name}"
                                ),
                                "required_tier": required_tier.name,
                                "actual_tier": user_tier.name,
                                "triggered_by_event_id": triggered_event_id,
                                "triggered_by_user_id": triggered_user_id,
                            },
                        )
                        continue
                # ─── 通过 ───
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
                        # audit trail：让下游（ToolWorker / 历史回放 / 安全审计）
                        # 都能看到本次 dispatch 凭谁触发、tier 几何
                        "triggered_by_event_id": triggered_event_id,
                        "triggered_by_user_id": triggered_user_id,
                        "triggered_by_user_tier": user_tier.name,
                    },
                )
                # 叫醒 ToolWorker 立即执行（同 reply 那条线的推+拉策略）
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

    敏感工具调用没填 triggered_by_event_id 时，AgentLoop 闸门 fall back 到
    "调用挂的 task 是哪条消息触发的" —— 这与 create_task 的 anchor 语义一
    致：task 是"我要给小王查天气"，那 task 内任何敏感操作都视作小王的指
    令。task 不存在或没填 anchor 时返回 None（敏感工具据此判失败）。
    """
    for t in context.active_tasks:
        if t.task_id == task_id:
            return t.triggered_by_event_id
    return None


def _validate_decision(decision: DecisionOutput, *, scope_key: str) -> str | None:
    """Return a short error string on invalid output, or None if valid.

    Rules (任务与决策契约 §3.1, §3.2.3):
    - IdleAction never co-exists with another action.

    Reply 校验下沉到 ReplyTool.run() —— 由于 reply 现在是普通工具，
    target.kind/group_id 与 scope_key 不匹配时 tool 自身 raise，
    ToolWorker 写 agent.tool_failed；"一 tick 多回复"的硬约束移除，
    由 group_chat_rules.md 软规范引导（多发短消息有时是合理选择）。
    """
    actions = decision.actions
    if any(isinstance(a, IdleAction) for a in actions) and len(actions) > 1:
        return "idle_with_other_actions"
    return None

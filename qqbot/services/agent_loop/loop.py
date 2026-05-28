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

        await self._apply_actions(decision.actions, correlation_id, decision_id)

        await self._write_tick_ended(
            correlation_id, tick_started_id, actions_count=len(decision.actions)
        )

    async def _apply_actions(
        self,
        actions: list[Action],
        correlation_id: str,
        decision_id: str,
    ) -> None:
        """Translate every action into agent.* events.

        Maintains an in-tick task_ref → task_id map so a CallToolAction can
        attach to a task created in the same actions list.
        """
        ref_to_task_id: dict[str, str] = {}

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

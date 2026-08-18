"""Long-running per-scope loop for program-shaped Planner decisions.

Each tick projects the scope, asks the Planner for one restricted Python
program, preflights it, and on static failure cools the current LLM endpoint
then tries the next candidate in the role group (no same-tick rewrite with
validation feedback). The decision root is always persisted.

模型每拍的输出在结构上解耦为**两层**（2026-08-17 提案-裁决流水线 §1.0）：

- **裁决层**（调度元指令）：``execute_decision(event_id=…)``，告诉调度器把某条
  历史决策事件提交给 Runner 执行。至多一条，可以没有。
- **动作层**（业务程序代码）：这一拍新写的 Python 代码，当拍一个函数都不跑，
  只作为新事件落库。

两层完全正交，四种组合都合法::

    ① 两层皆空     写 program_completed，**不唤醒** —— 唯一停止符   (completed)
    ② 纯提案       动作层落库，不派发；本拍自己再开一拍去审阅       (proposed)
    ③ 纯裁决       派发被引用的那条决策，等它的 terminal 接力       (dispatched)
    ④ 流水线混合   派发 + 新代码落库；唤醒同样交给 terminal 接力    (dispatched)

落库解耦（§1.1 防套娃）：preflight 把裁决指令从源码里剥掉，
``decision_emitted.payload.program`` 只存纯业务代码。

由此没有任何一段有副作用的程序会在模型只看过一次世界的情况下跑起来：写下它的那
一拍和让它生效的那一拍之间，必然隔着一次重新读时间线。④ 让这次多出来的推理被
摊掉——稳态下每拍既确认上一段又写下一段。

A per-scope ProgramRunner runs committed programs concurrently (one coroutine
each, calls inside one program stay sequential) and wakes the loop when a
program terminal is written.

**Every** wake goes through the same fixed batching window — external ingest,
proposal self-wake and Runner completion alike. The first wake opens one bounded
window; later wakes join it without extending the deadline, so split QQ messages
can land before the next decision starts. That window is exactly what makes the
extra tick worth having: the half sentence a human is still typing arrives in it.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from datetime import datetime
from typing import Any, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from qqbot.core.ids import new_event_id
from qqbot.core.logging import get_logger
from qqbot.core.settings import get_env_value
from qqbot.core.time import china_now
from qqbot.services.agent_loop.decision import (
    DecisionContext,
    DecisionOutput,
    Planner,
)

# 同拍最多决定次数：每次 preflight 失败冷却当前端点后换组内下一个模型再
# decide；**不**把被拒源码/校验错误喂回模型（2026-08-11 取消「校验拒绝」纠错环）。
_DECIDE_MAX_ATTEMPTS = 3
from qqbot.services.agent_loop.event_writer import (
    write_agent_event,
    write_runtime_event,
)
from qqbot.services.agent_loop.program_ast import (
    PreflightResult,
    ProgramErrorInfo,
    ProgramPreflightError,
    preflight,
)
from qqbot.services.agent_loop.program_events import (
    load_referenced_decision,
    recover_interrupted_programs,
    write_program_completed,
    write_program_failed,
)
from qqbot.services.agent_loop.program_runner import ProgramRunner, QueuedProgram
from qqbot.services.agent_loop.program_runtime import (
    ProgramExecutionError,
    ProgramExecutor,
    ProgramTrace,
)
from qqbot.services.agent_loop.projection import Projector
from qqbot.services.agent_loop.tool_registry import ToolRegistry

logger = get_logger(__name__)

# 唤醒攒批窗口（2026-07-28 引入，2026-08-01 由滑动改固定，见模块 docstring）。
# 第一次唤醒开窗，窗口内的后续唤醒并入本窗、不顺延 deadline，到点开一拍——
# 开拍延迟因此天然有界（≤ 窗口本身），不再需要防饿死的封顶常量。
_WAKE_BATCH_WINDOW_SECONDS = 3.0

# 自续拍（2026-08-04）：一段程序只要真的调用过函数，本拍收尾后立刻再开一拍。
#
# 动机——程序是**盲写**的：写下它的那一刻结果还不存在。文法虽有 if/for，那只
# 够对结果做机械分支；「读懂 search_history 拿回来的二十条再决定说什么」这类
# 需要判断力的事，当拍无论如何写不出来。而在此之前，那下一拍除非群里恰好又有
# 人说话否则永远不来，于是所有查完要接着办的链路都断在原地。
#
# 终止条件是不动点：某一拍的程序一个函数都没调用（空程序、或只有赋值与注释），
# 链条自然结束——恰好就是「没什么可做」的既有输出形态，不需要新概念。
#
# 抑制规则一条都没有（2026-08-04 明确决定）：`wait` 这类自带定时器的调用同样
# 续拍，她可能反复改期或反复记任务自转，守住这条的只有提示词纪律。
# 2026-08-17 起自续拍的口径扩大：提案拍与裁决报错拍也走它（那两种拍没有后台任务
# 替它们唤醒），因此一次「提案→裁决→执行→再决策」正常就要吃掉 3 层深度——
# 配这个 env 时按此估算，别照旧按「一次查询一层」算。上界默认不设。
_CONTINUATION_MAX_ENV = "AGENT_CONTINUATION_MAX_TICKS"

SessionFactory = Callable[[], AsyncSession]


def continuation_max_ticks() -> int | None:
    """连续自续拍上界。env ``AGENT_CONTINUATION_MAX_TICKS``。

    未配置 / 空 → ``None``，即**不限制**（当前默认）；``0`` → 关掉自续拍，
    退回纯事件驱动；``N > 0`` → 一段活动内最多连续自续 N 拍，之后必须等一次
    真正的外部唤醒。计数被任何外部唤醒清零，因此约束的是「一次自转能有多长」，
    不是「一小时能跑多少拍」。

    留这个旋钮是因为默认无界：真在生产里自转起来时，部署侧不改代码就能收。
    """
    raw = get_env_value(_CONTINUATION_MAX_ENV)
    if raw is None or not str(raw).strip():
        return None
    try:
        value = int(str(raw).strip())
    except ValueError:
        logger.warning(
            "[loop] invalid {}={!r}, treating as unlimited",
            _CONTINUATION_MAX_ENV,
            raw,
        )
        return None
    return max(value, 0)


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
        # supervisor 鸭子类型注入，规避 supervisor → loop 的循环 import；
        # 程序内 wait 等工具仍用它的 wake / note_activity 回调。
        self._supervisor = supervisor
        # bot_user_id 每 tick 重新 resolve —— bot 重连后 self_id 不变但实例
        # 会换；启动初期可能返回 None，prompt 渲染层接受 None 优雅降级。
        # None resolver 表示不注入（旧测试 / 早期骨架兼容）。
        self._bot_user_id_resolver = bot_user_id_resolver
        # Registry 是唯一 Program API。权限/scope/role 判定仍全部下放工具内
        # BaseTool.enforce_access；空 registry 只允许空程序与安全 builtin。
        self._tool_registry = (
            tool_registry if tool_registry is not None else ToolRegistry()
        )
        # 可选的图片描述依赖会随 ProgramExecutor context 注入工具。
        self._caption_image = caption_image
        self._wake = asyncio.Event()
        self._stopped = False
        self._tick_seq = 0
        self._task: asyncio.Task[None] | None = None
        # 攒批窗口状态：deadline 是当前窗口的到点时刻，开窗时算一次、窗口内
        # 不再变动（None = 当前没开窗，下一次 wake() 负责开）；timer 是正在睡
        # 到那个时刻的协程，同一时刻至多一个。
        self._wake_deadline: float | None = None
        self._wake_timer: asyncio.Task[None] | None = None
        # 每个 loop 实例的第一拍、投影之前收口一次历史半截程序；成功后不重跑。
        self._recovery_done = False
        # Runner 完成 wake 计入自续拍深度；外部 wake 清零。上界仍是
        # AGENT_CONTINUATION_MAX_TICKS，防止「跑完→决策→再入队」自转。
        self._continuation_depth = 0
        self._continuation_max = continuation_max_ticks()
        self._runner = ProgramRunner(
            scope_key=scope_key,
            execute=self._run_queued_program,
            on_finished=self._wake_continuation,
        )

    @property
    def scope_key(self) -> str:
        return self._scope_key

    def start(self) -> None:
        if self._task is not None:
            return
        self._runner.start()
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
        await self._runner.stop()

    def wake(self) -> None:
        """请求开拍。外部入口，走固定攒批窗口（见模块 docstring）。

        EventIngest / wait 到点 / 静默叫醒都经此入口。顺带把自续拍计数清零：
        外面又有事发生，上一段自转到此为止。自续拍走 ``_wake_continuation``，
        不经过这里——差别只在计数，不在窗口。
        """
        if self._stopped:
            return
        self._continuation_depth = 0
        self._arm_wake()

    def _wake_continuation(self) -> bool:
        """本 loop 自己刚落了事实、需要再开一拍。返回是否真的排上。

        两个来源：决策拍自己写完提案 / 裁决报错，以及 Runner 写出 program
        terminal。**与外部唤醒同一条窗口**（2026-08-17 维护者裁定）：所有落库
        的事件都走 3 秒攒批窗，没有旁路——决策事件和别的事件一样，凭什么它
        引发的那次唤醒可以插队。这三秒不是等待成本，正是人补完后半句的时间，
        跳过它就等于让下一拍照旧看不见新消息。

        与 ``wake()`` 的唯一区别是计数：自转不清零，受
        ``AGENT_CONTINUATION_MAX_TICKS`` 约束。
        """
        if self._stopped:
            return False
        if self._continuation_max is not None:
            if self._continuation_depth >= self._continuation_max:
                if self._continuation_max > 0:
                    logger.info(
                        "[loop {}] continuation capped at {} tick(s)",
                        self._scope_key,
                        self._continuation_max,
                    )
                return False
        self._continuation_depth += 1
        self._arm_wake()
        return True

    def _arm_wake(self) -> None:
        """唤醒排程本体。不碰自续拍计数——由两个入口各自负责。

        没有 immediate 旁路：唤醒只有这一条路径。``_WAKE_BATCH_WINDOW_SECONDS
        <= 0`` 是测试用的关窗档。
        """
        if _WAKE_BATCH_WINDOW_SECONDS <= 0:
            self._cancel_wake_timer()
            self._wake_deadline = None
            self._wake.set()
            return

        if self._wake_deadline is not None:
            # 窗口已开：本次唤醒并入这一拍，**不**顺延 deadline。
            return
        self._wake_deadline = time.monotonic() + _WAKE_BATCH_WINDOW_SECONDS
        if self._wake_timer is None or self._wake_timer.done():
            self._wake_timer = asyncio.create_task(
                self._wake_after_window(),
                name=f"agent_loop_wake:{self._scope_key}",
            )

    async def _wake_after_window(self) -> None:
        """睡到窗口到点再置位。deadline 在开窗那一刻就定死、窗口内不会被后续
        唤醒推后，所以只睡一次即可（旧的滑动实现要在这里重读 deadline 续睡）。
        """
        deadline = self._wake_deadline
        if deadline is None:
            return
        remaining = deadline - time.monotonic()
        if remaining > 0:
            await asyncio.sleep(remaining)
        self._wake_deadline = None
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
        tick_started_id = await write_runtime_event(
            self._session_factory,
            event_type="runtime.tick_started",
            scope_key=self._scope_key,
            visibility="runtime_only",
            correlation_id=correlation_id,
            causation_id=None,
            payload={"tick_seq": self._tick_seq},
        )
        if not self._recovery_done:
            report = await recover_interrupted_programs(
                self._session_factory,
                scope_key=self._scope_key,
            )
            self._recovery_done = True
            if report.tool_calls_closed:
                logger.warning(
                    "[loop {}] recovered interrupted tool call(s): {}",
                    self._scope_key,
                    report.tool_calls_closed,
                )

        context = await self._build_context(correlation_id, now)
        decision, prepared, preflight_error = await self._decide_program(context)
        if decision is None:
            await self._write_tick_ended(
                correlation_id,
                tick_started_id,
                program_status="planner_error",
            )
            return

        if prepared is not None:
            stored_program = prepared.source
            program_sha256 = prepared.program_sha256
        else:
            stored_program = _bounded_program_source(decision.program)
            program_sha256 = _program_sha256(stored_program)

        decision_id = await write_agent_event(
            self._session_factory,
            event_type="agent.decision_emitted",
            scope_key=self._scope_key,
            correlation_id=correlation_id,
            causation_id=None,
            payload={
                "program": stored_program,
                "program_sha256": program_sha256,
                "tick_seq": self._tick_seq,
            },
            occurred_at=now,
        )

        if prepared is None:
            info = preflight_error or ProgramErrorInfo(
                "invalid_program_giveup",
                "program preflight failed after endpoint failover",
            )
            details = dict(info.details)
            if info.line is not None:
                details["line"] = info.line
            if info.column is not None:
                details["column"] = info.column
            await _shield_write(
                write_program_failed(
                    self._session_factory,
                    scope_key=self._scope_key,
                    correlation_id=correlation_id,
                    decision_id=decision_id,
                    program_sha256=program_sha256,
                    duration_ms=0,
                    query_calls=[],
                    effect_call_ids=[],
                    error_kind="invalid_program_giveup",
                    error_message=(
                        "program remained invalid after endpoint failover: "
                        f"{info.error_kind}: {info.message}"
                    ),
                    failed_call=None,
                    rejected_error_kind=info.error_kind,
                    **details,
                )
            )
            await self._write_tick_ended(
                correlation_id,
                tick_started_id,
                program_status="invalid",
            )
            return

        # 两层各自独立结算（§1.0）：裁决层调度的是**别的**事件，动作层是本拍新写
        # 的代码，同一次输出里可以两者都有，也可以只有一个、一个都没有。
        commit_outcome: str | None = None
        if prepared.commit_event_id is not None:
            commit_outcome = await self._commit_decision(
                commit_decision_id=decision_id,
                commit_program_sha256=program_sha256,
                target_event_id=prepared.commit_event_id,
                correlation_id=correlation_id,
                context=context,
                now=now,
            )
        left_proposal = bool(prepared.call_sites or prepared.has_return)

        if commit_outcome is None and not left_proposal:
            # 两层都空 = 停止符：当拍收口，**不**唤醒，这段连续运行到此为止。
            await _shield_write(
                write_program_completed(
                    self._session_factory,
                    scope_key=self._scope_key,
                    correlation_id=correlation_id,
                    decision_id=decision_id,
                    program_sha256=program_sha256,
                    duration_ms=0,
                    query_calls=[],
                    effect_call_ids=[],
                    result=None,
                    has_result=prepared.has_return,
                )
            )
            await self._write_tick_ended(
                correlation_id,
                tick_started_id,
                program_status="completed",
                left_proposal=False,
            )
            return

        # 每个非空拍恰好唤醒一次：派发成功了就等被执行程序的 terminal 接力，
        # 否则本拍自己再开一拍（提案要有人来复核，裁决报错要让模型看见）。
        if commit_outcome != "dispatched":
            self._wake_continuation()
        await self._write_tick_ended(
            correlation_id,
            tick_started_id,
            program_status=commit_outcome or "proposed",
            left_proposal=left_proposal,
        )

    async def _commit_decision(  # noqa: PLR0913
        self,
        *,
        commit_decision_id: str,
        commit_program_sha256: str,
        target_event_id: str,
        correlation_id: str,
        context: DecisionContext,
        now: datetime,
    ) -> str:
        """裁决层：把被指名的那条历史决策交给 Runner 真正执行。

        被引用事件里存的本来就是纯业务代码（preflight 落库前已剥掉裁决层），
        因此这里重新 preflight 一遍拿到的必然 ``commit_event_id is None``，不存在
        套娃。

        被执行程序沿用它落库那一拍的 correlation_id：那些 ``tool_called`` /
        terminal 是这段程序的事件，归属它的出处，不归属按下执行键的这一拍。

        成功时本拍**不写**任何 program terminal——终态属于被执行的那段程序。
        失败按提案 §1.1 写 ``agent.program_failed``，挂在本拍的决策事件上。
        唤醒由调用方统一处理。

        已知副作用（实现时向维护者提出）：流水线混合拍里，本拍的决策事件同时
        承载着动作层新代码；给它扣上 program terminal 会让 ``already_executed``
        连那段新代码一起判死，模型只能照着时间线重写一遍。
        """
        decision, error_kind = await load_referenced_decision(
            self._session_factory,
            scope_key=self._scope_key,
            event_id=target_event_id,
        )
        if decision is None:
            await self._reject_commit(
                decision_id=commit_decision_id,
                program_sha256=commit_program_sha256,
                correlation_id=correlation_id,
                error_kind=error_kind or "decision_not_found",
                error_message=_COMMIT_REJECTION_MESSAGES.get(
                    error_kind or "",
                    f"cannot execute decision {target_event_id}",
                ),
                target_event_id=target_event_id,
            )
            return "commit_rejected"

        scope = self._scope_key.split(":", 1)[0]
        try:
            target = preflight(decision.program, self._tool_registry, scope)
        except ProgramPreflightError as exc:
            # 存量源码现在过不了预检——工具下线、scope 权限变了都会这样。
            # 报的是被引用程序自己的错，不是裁决语法错。
            await self._reject_commit(
                decision_id=commit_decision_id,
                program_sha256=commit_program_sha256,
                correlation_id=correlation_id,
                error_kind=exc.info.error_kind,
                error_message=(
                    f"referenced decision {target_event_id} no longer passes "
                    f"preflight: {exc.info.message}"
                ),
                target_event_id=target_event_id,
            )
            return "commit_rejected"

        if not (target.call_sites or target.has_return):
            # 那条决策的动作层是空的：它只下过一次调度指令，或者本来就是空程序
            # （空程序在它自己那一拍就收了终态）。没有代码可跑。
            await self._reject_commit(
                decision_id=commit_decision_id,
                program_sha256=commit_program_sha256,
                correlation_id=correlation_id,
                error_kind="decision_not_a_proposal",
                error_message=_COMMIT_REJECTION_MESSAGES["decision_not_a_proposal"],
                target_event_id=target_event_id,
            )
            return "commit_rejected"

        try:
            self._runner.enqueue(
                QueuedProgram(
                    decision_id=decision.event_id,
                    scope_key=self._scope_key,
                    correlation_id=decision.correlation_id or correlation_id,
                    prepared=target,
                    context=context,
                    enqueued_at=now,
                )
            )
        except Exception as exc:
            logger.exception("[loop {}] enqueue failed: {}", self._scope_key, exc)
            # 这一条终态写在**被执行**的那条决策上，而不是本拍：它这次真的没跑
            # 成，模型必须重写一段新的，不该再被指名。
            await _shield_write(
                write_program_failed(
                    self._session_factory,
                    scope_key=self._scope_key,
                    correlation_id=decision.correlation_id or correlation_id,
                    decision_id=decision.event_id,
                    program_sha256=target.program_sha256,
                    duration_ms=0,
                    query_calls=[],
                    effect_call_ids=[],
                    error_kind="dispatch_failed",
                    error_message=f"program enqueue failed: {type(exc).__name__}",
                )
            )
            return "failed"
        return "dispatched"

    async def _reject_commit(
        self,
        *,
        decision_id: str,
        program_sha256: str,
        correlation_id: str,
        error_kind: str,
        error_message: str,
        target_event_id: str,
    ) -> None:
        """裁决被拒：本拍写 ``agent.program_failed``（提案 §1.1）。"""
        await _shield_write(
            write_program_failed(
                self._session_factory,
                scope_key=self._scope_key,
                correlation_id=correlation_id,
                decision_id=decision_id,
                program_sha256=program_sha256,
                duration_ms=0,
                query_calls=[],
                effect_call_ids=[],
                error_kind=error_kind,
                error_message=error_message,
                target_event_id=target_event_id,
            )
        )

    async def _build_context(
        self,
        correlation_id: str,
        now: datetime,
    ) -> DecisionContext:
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
        if self._projector is not None:
            try:
                return await self._projector.build_context(
                    scope_key=self._scope_key,
                    correlation_id=correlation_id,
                    tick_seq=self._tick_seq,
                    now=now,
                    bot_user_id=bot_user_id,
                )
            except Exception as exc:
                logger.exception(
                    "[loop {}] projection failed; using empty context: {}",
                    self._scope_key,
                    exc,
                )
        return DecisionContext(
            scope_key=self._scope_key,
            correlation_id=correlation_id,
            tick_seq=self._tick_seq,
            now=now,
            bot_user_id=bot_user_id,
        )

    async def _decide_program(
        self,
        context: DecisionContext,
    ) -> tuple[
        DecisionOutput | None,
        PreflightResult | None,
        ProgramErrorInfo | None,
    ]:
        """decide → preflight；失败只冷却端点并换模型再 decide，不喂校验拒绝。"""
        last_decision: DecisionOutput | None = None
        last_error: ProgramErrorInfo | None = None
        scope = self._scope_key.split(":", 1)[0]
        for attempt in range(1, _DECIDE_MAX_ATTEMPTS + 1):
            try:
                last_decision = await self._planner.decide(context)
            except Exception as exc:
                logger.exception(
                    "[loop {}] planner failed: {}", self._scope_key, exc
                )
                return None, None, None
            try:
                prepared = preflight(
                    last_decision.program,
                    self._tool_registry,
                    scope,
                )
            except ProgramPreflightError as exc:
                last_error = exc.info
                reason = f"{exc.info.error_kind}:{exc.info.message}"
                self._report_invalid_output(reason)
                await write_runtime_event(
                    self._session_factory,
                    event_type="runtime.llm_invalid_output",
                    scope_key=self._scope_key,
                    visibility="agent_visible",
                    correlation_id=context.correlation_id,
                    causation_id=None,
                    payload={
                        "attempt": attempt,
                        "error_kind": exc.info.error_kind,
                        "error_message": exc.info.message,
                        "line": exc.info.line,
                        "column": exc.info.column,
                    },
                )
                continue
            return last_decision, prepared, None
        return last_decision, None, last_error

    def _report_invalid_output(self, reason: str) -> None:
        report = getattr(self._planner, "report_invalid_output", None)
        if not callable(report):
            return
        try:
            report(reason)
        except Exception as exc:
            logger.warning(
                "[loop {}] invalid-output route report failed: {}",
                self._scope_key,
                exc,
            )

    async def _run_queued_program(self, item: QueuedProgram) -> None:
        """Runner 回调：顺序执行一段已 preflight 的程序并写 program terminal。"""
        executor = ProgramExecutor(
            registry=self._tool_registry,
            session_factory=self._session_factory,
            scope_key=self._scope_key,
            correlation_id=item.correlation_id,
            decision_id=item.decision_id,
            context=item.context,
            supervisor=self._supervisor,
            caption_image=self._caption_image,
        )
        prepared = item.prepared
        try:
            result = await executor.execute(prepared)
        except ProgramExecutionError as exc:
            trace = exc.trace or ProgramTrace(
                decision_id=item.decision_id,
                program_sha256=prepared.program_sha256,
                scope_key=self._scope_key,
            )
            details = dict(exc.info.details)
            if exc.info.line is not None:
                details["line"] = exc.info.line
            if exc.info.column is not None:
                details["column"] = exc.info.column
            await _shield_write(
                write_program_failed(
                    self._session_factory,
                    scope_key=self._scope_key,
                    correlation_id=item.correlation_id,
                    decision_id=item.decision_id,
                    program_sha256=prepared.program_sha256,
                    duration_ms=trace.duration_ms,
                    query_calls=list(trace.query_calls),
                    effect_call_ids=list(trace.effect_call_ids),
                    error_kind=exc.info.error_kind,
                    error_message=exc.info.message,
                    failed_call=exc.failed_call_payload(),
                    **details,
                )
            )
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "[loop {}] program host failure escaped executor", self._scope_key
            )
            await _shield_write(
                write_program_failed(
                    self._session_factory,
                    scope_key=self._scope_key,
                    correlation_id=item.correlation_id,
                    decision_id=item.decision_id,
                    program_sha256=prepared.program_sha256,
                    duration_ms=0,
                    query_calls=[],
                    effect_call_ids=[],
                    error_kind="internal_tool_error",
                    error_message=f"program host failure: {type(exc).__name__}",
                )
            )
            return

        await _shield_write(
            write_program_completed(
                self._session_factory,
                scope_key=self._scope_key,
                correlation_id=item.correlation_id,
                decision_id=item.decision_id,
                program_sha256=prepared.program_sha256,
                duration_ms=result.trace.duration_ms,
                query_calls=list(result.trace.query_calls),
                effect_call_ids=list(result.trace.effect_call_ids),
                result=result.result,
                has_result=result.has_result,
            )
        )

    async def _write_tick_ended(
        self,
        correlation_id: str,
        tick_started_id: str,
        program_status: str,
        left_proposal: bool = False,
    ) -> None:
        """``program_status`` 记裁决层的结果，``left_proposal`` 记动作层有没有
        留下新代码——两层独立，一拍可以同时有。"""
        await write_runtime_event(
            self._session_factory,
            event_type="runtime.tick_ended",
            scope_key=self._scope_key,
            visibility="runtime_only",
            correlation_id=correlation_id,
            causation_id=tick_started_id,
            payload={
                "tick_seq": self._tick_seq,
                "program_status": program_status,
                "left_proposal": left_proposal,
            },
        )


# 裁决失败的说明文本。它会作为 ``<程序>失败`` 的「原因」行进入信封，是模型
# 唯一能看到的纠正依据——写成让人一眼知道下一步该干什么的话。
_COMMIT_REJECTION_MESSAGES = {
    "decision_not_found": (
        "no such decision in this scope; copy the id from a <程序>决策 row's "
        "ev: prefix in the current timeline"
    ),
    "already_executed": (
        "that decision is already running or has already finished; write a new "
        "program instead of running the same one twice"
    ),
    "decision_not_a_proposal": (
        "that decision has no program body to run (it only carried a scheduling "
        "directive, or it was an empty program that already closed)"
    ),
}


def _bounded_program_source(value: Any) -> str:
    from qqbot.services.agent_loop.program_ast import MAX_SOURCE_CHARS

    source = value if isinstance(value, str) else str(value)
    return source[:MAX_SOURCE_CHARS]


def _program_sha256(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


async def _shield_write(awaitable: Any) -> None:
    """Let a terminal transaction finish even if loop shutdown cancels the tick."""
    task = asyncio.create_task(awaitable)
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    task.result()

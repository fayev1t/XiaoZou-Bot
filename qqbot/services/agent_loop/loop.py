"""Long-running per-scope loop for program-shaped Planner decisions.

Each tick projects the scope, asks the Planner for one restricted Python
program, preflights it (up to three same-tick attempts), persists the decision
root, and executes every query/effect sequentially before the tick ends.
There is no worker dispatch or tool batch: a completed tick has no pending
program call.

The fixed wake batching window remains a conversation-ingest concern. The
first wake opens one bounded window; later wakes join it without extending the
deadline, so split QQ messages can land before the next decision starts.

A tick whose program actually called something wakes the scope again on its
own (see ``_CONTINUATION_MAX_ENV``), so a burst of work runs tick by tick until
one program calls nothing at all.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import replace
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
    ProgramValidationFeedback,
)
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
    recover_interrupted_programs,
    write_program_completed,
    write_program_failed,
)
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
# 抑制规则一条都没有（2026-08-04 明确决定）：wait / reply 这类自带定时器的调用
# 同样续拍。已知代价有二——她可能在 reply 的 hold 尚未到点时被立刻叫回来，从而
# 绕过两步发言；也可能反复改期或反复记任务自转。守住这两条的现在只有提示词纪律，
# 没有结构性保障。上界默认不设，全靠下面这个 env 兜底。
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
        # 程序内 reply/wait 等工具仍用它的 wake/notify_reply_task 回调。
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
        # 自续拍状态：depth 是本段活动里已经连续自续了几拍，被任何外部唤醒清零；
        # max 在构造时读一次 env（进程内不会变）。
        self._continuation_depth = 0
        self._continuation_max = continuation_max_ticks()

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

    def wake(self) -> None:
        """请求开拍。外部入口一律走固定攒批窗口（见模块 docstring）。

        EventIngest / wait 到点 / reply 完成 / 静默叫醒都经此入口；统一 3s
        窗口，不再区分 immediate。顺带把自续拍计数清零：外面又有事发生，上一段
        自转到此为止。自续拍走 ``_wake_continuation``，不经过这里。
        """
        if self._stopped:
            return
        self._continuation_depth = 0
        self._arm_wake(immediate=False)

    def _wake_continuation(self) -> bool:
        """本拍程序调用过函数 → 立刻再开一拍。返回是否真的排上。

        自续拍是 loop 内部排程，不走公开 wake：不是在等谁把话说完，攒批窗口
        在这里只是每跳白加三秒。
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
        self._arm_wake(immediate=True)
        return True

    def _arm_wake(self, *, immediate: bool) -> None:
        """唤醒排程本体。不碰自续拍计数——由两个入口各自负责。

        ``immediate=True`` 仅供自续拍；外部一律 ``False`` 进攒批窗口。
        """
        if immediate or _WAKE_BATCH_WINDOW_SECONDS <= 0:
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
            if report.tool_calls_closed or report.programs_closed:
                logger.warning(
                    "[loop {}] recovered interrupted calls={} programs={}",
                    self._scope_key,
                    report.tool_calls_closed,
                    report.programs_closed,
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
                "program preflight failed after three attempts",
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
                        "program remained invalid after three attempts: "
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

        status, made_call = await self._execute_program(
            prepared,
            context=context,
            correlation_id=correlation_id,
            decision_id=decision_id,
        )
        await self._write_tick_ended(
            correlation_id,
            tick_started_id,
            program_status=status,
        )
        # 自续拍：排在 tick_ended 之后，本拍的事实全部落库才请求下一拍——与
        # 「wake 不能领先于事实」同序。上面两条提前 return 的路径（planner_error /
        # invalid）一个函数都没调用过，天然不续，无需另写分支。
        if made_call:
            self._wake_continuation()

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
        feedback: ProgramValidationFeedback | None = None
        last_decision: DecisionOutput | None = None
        last_error: ProgramErrorInfo | None = None
        scope = self._scope_key.split(":", 1)[0]
        for attempt in range(1, 4):
            attempt_context = (
                context
                if feedback is None
                else replace(context, validation_feedback=feedback)
            )
            try:
                last_decision = await self._planner.decide(attempt_context)
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
                feedback = ProgramValidationFeedback(
                    attempt=attempt,
                    error_kind=exc.info.error_kind,
                    message=exc.info.message,
                    rejected_program=_bounded_program_source(
                        last_decision.program
                    ),
                    line=exc.info.line,
                    column=exc.info.column,
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

    async def _execute_program(
        self,
        prepared: PreflightResult,
        *,
        context: DecisionContext,
        correlation_id: str,
        decision_id: str,
    ) -> tuple[str, bool]:
        """执行程序。返回 (program_status, 本拍是否调用过函数)。

        第二项是自续拍的唯一判据：``trace.calls`` 对 query / effect、成功 / 失败
        一视同仁地记录（program_runtime._record_call），所以「调用过」按字面成立
        ——查询也算，失败也算。失败尤其要算：那一拍的价值正是让她看见错误再判断，
        而中止余下程序意味着她当拍不可能自己接住。
        """
        executor = ProgramExecutor(
            registry=self._tool_registry,
            session_factory=self._session_factory,
            scope_key=self._scope_key,
            correlation_id=correlation_id,
            decision_id=decision_id,
            context=context,
            supervisor=self._supervisor,
            caption_image=self._caption_image,
        )
        try:
            result = await executor.execute(prepared)
        except ProgramExecutionError as exc:
            trace = exc.trace or ProgramTrace(
                decision_id=decision_id,
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
                    correlation_id=correlation_id,
                    decision_id=decision_id,
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
            return "failed", bool(trace.calls)
        except Exception as exc:  # noqa: BLE001
            # 兜底:执行器契约之外的宿主异常也必须留下 program terminal,
            # 否则 decision_emitted 悬空、tick_ended 不写,而收口器只在
            # 进程首拍跑一次,同进程内永远无人补写这一拍。
            logger.exception(
                "[loop {}] program host failure escaped executor", self._scope_key
            )
            await _shield_write(
                write_program_failed(
                    self._session_factory,
                    scope_key=self._scope_key,
                    correlation_id=correlation_id,
                    decision_id=decision_id,
                    program_sha256=prepared.program_sha256,
                    duration_ms=0,
                    query_calls=[],
                    effect_call_ids=[],
                    error_kind="internal_tool_error",
                    error_message=f"program host failure: {type(exc).__name__}",
                )
            )
            # 执行器契约之外的宿主异常：没有 trace，无从证实调用发生过。这里
            # 保守地不续拍——真有持续性宿主 bug 时，续拍只会把它变成自转。
            return "failed", False

        await _shield_write(
            write_program_completed(
                self._session_factory,
                scope_key=self._scope_key,
                correlation_id=correlation_id,
                decision_id=decision_id,
                program_sha256=prepared.program_sha256,
                duration_ms=result.trace.duration_ms,
                query_calls=list(result.trace.query_calls),
                effect_call_ids=list(result.trace.effect_call_ids),
                result=result.result,
                has_result=result.has_result,
            )
        )
        return "completed", bool(result.trace.calls)

    async def _write_tick_ended(
        self,
        correlation_id: str,
        tick_started_id: str,
        program_status: str,
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
                "program_status": program_status,
            },
        )


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

"""SilenceWatcher —— 群里彻底安静下来时，叫醒一次让她回想。

设计动机（2026-08-03）：她只在有事发生时被叫醒，于是"最近这段我做得怎么样"
这类回看永远没有发生的时机——被叫醒的那一拍总有一件正在推着她的事。而恰恰是
**没人说话**的时候才谈得上回看，那个时刻在现有链路里完全不产生事件，因此也
不产生唤醒：她根本不知道时间过去了。本模块补上这一个叫醒。

它只做一件事：**陈述"已经静默这么久了"这个事实**，然后开一拍。要不要因此回想、
要不要顺便说句话，全由 `planner.md` 的政策层和她自己决定——注入行绝不写成
"请调用 reflect"。时间线里的一切都不是给 Planner 的系统指令（`planner.md`
§系统运行方式），这条是现在唯一的防注入结构性保障，运行时自己不能第一个破例。

触发纪律：
  - 每个 group scope 一个计时器；**可见事实落库**时 ``notify_activity`` 重新
    武装（announce / EventIngest 提交后，不经 wake）；
  - 一段静默**只响一次**——响过之后不再自动重排，直到下一次活动重新武装；
    ``runtime.silence_elapsed`` 自身不算动静，不会 note_activity；
  - 到点先**回库复核**真实静默时长再决定响不响。复核兜底"计时器与库内最后
    可见时间不同步"的缝隙，误报只表现为推迟。

只覆盖 ``group:*``：system loop 没有聊天面，"群里安静了"对它没有意义；private
loop 从不实例化。

可靠性等级与 ``wait`` 工具一致——计时器只活在进程内存里，**进程重启即丢**，
不建持久化调度表。丢了的后果仅仅是这一段静默没人叫醒，下一条消息照常唤醒。
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from sqlalchemy import select

from qqbot.core.logging import get_logger
from qqbot.core.settings import get_env_value
from qqbot.core.time import china_now, normalize_china_time
from qqbot.models.agent_event import AgentEvent
from qqbot.services.agent_loop.event_writer import (
    SessionFactory,
    announce,
    parse_scope_key,
)

logger = get_logger(__name__)

SILENCE_EVENT_TYPE = "runtime.silence_elapsed"

DEFAULT_SILENCE_SECONDS = 600
# 复核时允许的抖动：真实静默差这点秒数就当已经到点，避免为了几秒钟再排一次。
_RECHECK_SLACK_SECONDS = 5

WakeCallback = Callable[[str], Awaitable[None]]


def silence_seconds() -> int:
    """静默阈值（秒）。env ``SILENCE_REFLECTION_SECONDS``，缺省 600。

    ``<= 0`` 表示关闭本机制——部署侧不改代码就能停掉这条叫醒。
    """
    raw = get_env_value("SILENCE_REFLECTION_SECONDS")
    if raw is None or not str(raw).strip():
        return DEFAULT_SILENCE_SECONDS
    try:
        return int(str(raw).strip())
    except ValueError:
        logger.warning(
            "[silence] invalid SILENCE_REFLECTION_SECONDS={!r}, using {}",
            raw,
            DEFAULT_SILENCE_SECONDS,
        )
        return DEFAULT_SILENCE_SECONDS


class SilenceWatcher:
    """按 scope 维护静默计时器。由 LoopSupervisor 持有并转发活动通知。"""

    def __init__(
        self,
        session_factory: SessionFactory,
        wake: WakeCallback,
        *,
        seconds: int | None = None,
    ) -> None:
        self._session_factory = session_factory
        # wake 只负责开拍；武装由外部在可见事实落库时调用 notify_activity。
        # silence_elapsed 写成功后不会 note_activity，因此不会自重置。
        self._wake = wake
        self._seconds = silence_seconds() if seconds is None else seconds
        self._timers: dict[str, asyncio.Task[None]] = {}
        self._stopped = False

    @property
    def enabled(self) -> bool:
        return self._seconds > 0

    def notify_activity(self, scope_key: str) -> None:
        """该 scope 时间线有可见动静：取消旧计时器，重新武装一个。

        非 group scope 与关闭态直接忽略。同步方法——挂在 announce / ingest 提交
        后的热路径上，不能引入 await。
        """
        if self._stopped or not self.enabled:
            return
        if not scope_key.startswith("group:"):
            return
        self._cancel(scope_key)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # 没有事件循环（同步测试装配）——静默叫醒不可用，不报错
        self._timers[scope_key] = loop.create_task(
            self._sleep_then_fire(scope_key),
            name=f"silence_watcher:{scope_key}",
        )

    async def stop(self) -> None:
        self._stopped = True
        for scope_key in list(self._timers):
            self._cancel(scope_key)

    def _cancel(self, scope_key: str) -> None:
        task = self._timers.pop(scope_key, None)
        if task is not None and not task.done():
            task.cancel()

    async def _sleep_then_fire(self, scope_key: str) -> None:
        """睡到点 → 回库复核 → 落事实事件 → 叫醒一次。

        复核不通过时按剩余时长再睡一轮（不是重新武装：仍是同一段静默，
        "只响一次"的约束不受影响）。
        """
        remaining = self._seconds
        try:
            while remaining > 0:
                await asyncio.sleep(remaining)
                if self._stopped:
                    return
                remaining = await self._recheck(scope_key)
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001 —— 叫醒失败绝不能拖垮别的 scope
            logger.warning("[silence] {} recheck failed: {}", scope_key, exc)
            return

        try:
            await self._emit_silence_elapsed(scope_key)
        finally:
            # 响过即卸下：这段静默不再自动重排，等下一次 notify_activity。
            self._timers.pop(scope_key, None)

    async def _emit_silence_elapsed(self, scope_key: str) -> None:
        """到点产出静默事实。有入口网关就走登记+静默门；否则退回 announce。

        写失败仍然叫醒（与 wait 同型：本体是「到点我会来」）。
        """
        from qqbot.services.event_gateway.outbound import get_inbound_gateway

        gateway = get_inbound_gateway()
        if gateway is not None:
            try:
                _scope, group_id, _user_id = parse_scope_key(scope_key)
                result = await gateway.submit(
                    "other",
                    {
                        "event_type": SILENCE_EVENT_TYPE,
                        "scope": "group",
                        "group_id": group_id,
                        "visibility": "agent_visible",
                        "seconds": self._seconds,
                    },
                )
            except Exception as exc:
                logger.warning(
                    "[silence] {} gateway submit failed (still waking): {}",
                    scope_key,
                    exc,
                )
                await self._wake(scope_key)
                return
            if getattr(result, "status", None) == "error":
                await self._wake(scope_key)
            return

        await announce(
            self._session_factory,
            event_type=SILENCE_EVENT_TYPE,
            scope_key=scope_key,
            visibility="agent_visible",
            correlation_id=None,
            causation_id=None,
            payload={"seconds": self._seconds},
            wake=self._wake,
            wake_on_write_failure=True,
        )

    async def _recheck(self, scope_key: str) -> int:
        """回库看真实静默时长；返回还需再睡的秒数（0 = 可以响了）。"""
        last = await self._last_visible_at(scope_key)
        if last is None:
            return 0
        elapsed = (china_now() - normalize_china_time(last)).total_seconds()
        gap = self._seconds - elapsed
        return int(gap) if gap > _RECHECK_SLACK_SECONDS else 0

    async def _last_visible_at(self, scope_key: str) -> Any | None:
        try:
            scope, group_id, user_id = parse_scope_key(scope_key)
        except ValueError:
            return None
        stmt = (
            select(AgentEvent.occurred_at)
            .where(AgentEvent.scope == scope)
            .where(AgentEvent.visibility == "agent_visible")
        )
        if group_id is not None:
            stmt = stmt.where(AgentEvent.group_id == group_id)
        if user_id is not None:
            stmt = stmt.where(AgentEvent.user_id == user_id)
        stmt = stmt.order_by(AgentEvent.occurred_at.desc()).limit(1)
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            return result.scalars().first()

"""LoopSupervisor — process-wide registry and lifecycle manager for AgentLoops.

Contract:
- 事件系统设计.md §10.3
- EventIngest契约.md §5.1

Behaviour:
- One SystemAgentLoop is created up front (on start()).
- GroupAgentLoops are lazy: instantiated on first wake("group:<id>").
- PrivateAgentLoop is NOT instantiated (实例化策略 §10.1); wake() silently
  drops scope_key="private:*".
- wake() before start() is a no-op (events keep accumulating in
  agent_events; the loop will see them once it tickets).
- stop() cancels every running loop with a 5s grace timeout.
"""

from __future__ import annotations

import asyncio
from typing import Callable

from sqlalchemy.ext.asyncio import AsyncSession

from qqbot.core.logging import get_logger
from qqbot.services.agent_loop import bot_registry
from qqbot.services.agent_loop.decision import Planner
from qqbot.services.agent_loop.loop import AgentLoop
from qqbot.services.agent_loop.projection import Projector
from qqbot.services.agent_loop.reply_worker import ReplySendWorker
from qqbot.services.agent_loop.tool_registry import ToolRegistry
from qqbot.services.agent_loop.tool_worker import ToolWorker

logger = get_logger(__name__)

SessionFactory = Callable[[], AsyncSession]


class LoopSupervisor:
    def __init__(
        self,
        planner: Planner,
        session_factory: SessionFactory,
        projector: Projector | None = None,
        tool_registry: ToolRegistry | None = None,
    ) -> None:
        self._planner = planner
        self._session_factory = session_factory
        self._projector = projector
        self._tool_registry = tool_registry
        self._loops: dict[str, AgentLoop] = {}
        self._lock = asyncio.Lock()
        self._started = False
        self._stopped = False
        self._reply_worker: ReplySendWorker | None = None
        self._tool_worker: ToolWorker | None = None

    @property
    def started(self) -> bool:
        return self._started

    @property
    def loop_count(self) -> int:
        return len(self._loops)

    async def start(self) -> None:
        if self._started or self._stopped:
            return
        # 任务读模型回填：把最近 N 天未完成任务灌进 agent_tasks，覆盖"首次部署
        # 本特性"和"读模型漂移"。在拉起任何 loop 之前跑，让 system/group loop
        # 第一 tick 就能从表里看到窗口外的旧任务。best-effort，失败不挡启动。
        try:
            from qqbot.services.agent_loop.task_store import backfill_recent

            replayed = await backfill_recent(self._session_factory)
            logger.info(
                "[supervisor] task read-model backfill: {} task event(s) replayed",
                replayed,
            )
        except Exception as exc:
            logger.warning(
                "[supervisor] task backfill failed (continuing): {}", exc
            )
        # 先把 ReplySendWorker 跑起来：保证 SystemAgentLoop 第一 tick 出
        # reply 时也能马上发出。worker 自身 start() 即触发一次 catchup。
        self._reply_worker = ReplySendWorker(session_factory=self._session_factory)
        self._reply_worker.start()
        # ToolWorker：只有注入 registry 才启动；catchup 同 reply worker。
        # 把自己注入进去让 worker 在 drain 完后自驱唤醒对应 scope 的 AgentLoop
        # （拓扑 README §5.3 修复）。
        if self._tool_registry is not None:
            self._tool_worker = ToolWorker(
                session_factory=self._session_factory,
                registry=self._tool_registry,
                supervisor=self,
            )
            self._tool_worker.start()
        # SystemAgentLoop wakes up to handle scope=system events
        # (request.*, lifecycle, bot_offline, ...).
        await self._ensure("system")
        self._started = True
        logger.info(
            "[supervisor] started, system loop + reply worker + tool worker={} online",
            "yes" if self._tool_worker is not None else "no",
        )

    async def stop(self) -> None:
        self._stopped = True
        loops = list(self._loops.values())
        self._loops.clear()
        await asyncio.gather(
            *(loop.stop() for loop in loops), return_exceptions=True
        )
        if self._reply_worker is not None:
            try:
                await self._reply_worker.stop()
            except Exception as exc:
                logger.warning("[supervisor] reply_worker.stop failed: {}", exc)
            finally:
                self._reply_worker = None
        if self._tool_worker is not None:
            try:
                await self._tool_worker.stop()
            except Exception as exc:
                logger.warning("[supervisor] tool_worker.stop failed: {}", exc)
            finally:
                self._tool_worker = None
        logger.info("[supervisor] stopped, {} loops drained", len(loops))

    async def wake(self, scope_key: str) -> None:
        if self._stopped:
            return
        if scope_key.startswith("private:"):
            # 实例化策略 §10.1: private 不实例化 loop
            return
        try:
            loop = await self._ensure(scope_key)
        except ValueError:
            logger.warning("[supervisor] invalid scope_key: {}", scope_key)
            return
        loop.wake()

    def notify_reply_pending(self) -> None:
        """AgentLoop 写完 reply_emitted 后调，叫醒 ReplySendWorker。

        无 worker（未 start / 已 stop）时静默忽略；catchup 流程保证
        worker 启动时会扫到这条遗留事件。
        """
        if self._reply_worker is not None:
            self._reply_worker.notify()

    def notify_tool_pending(self) -> None:
        """AgentLoop 写完 tool_called 后调，叫醒 ToolWorker。语义同
        notify_reply_pending；未注入 tool_registry 时是 no-op。"""
        if self._tool_worker is not None:
            self._tool_worker.notify()

    async def _ensure(self, scope_key: str) -> AgentLoop:
        async with self._lock:
            existing = self._loops.get(scope_key)
            if existing is not None:
                return existing
            loop = AgentLoop(
                scope_key=scope_key,
                planner=self._planner,
                session_factory=self._session_factory,
                projector=self._projector,
                supervisor=self,
                bot_user_id_resolver=_default_bot_user_id_resolver,
                tool_registry=self._tool_registry,
            )
            loop.start()
            self._loops[scope_key] = loop
            logger.info("[supervisor] loop spawned: {}", scope_key)
            return loop


def _default_bot_user_id_resolver() -> str | None:
    """单 bot 部署的默认 resolver：从 bot_registry 取第一个已注册 self_id。

    多账号场景（同一进程同时注册多个 Bot 实例）下应当按 scope_key 选合适的
    bot——比如这个群里 bot A 是成员、bot B 不是——但目前 v2 还没有 scope →
    bot 的路由表，先用单 bot 假设兜底，等真有多账号需求时再细化。

    返回 None 时（启动初期，nonebot 还没把 Bot 注册进来）AgentLoop 把
    bot_user_id 保持为 None，prompt 渲染层不输出该属性；此时 LLM 仍可靠别人
    <reply ... from="我(...)"/> 的自指标签识别"这条是回复我的"——这是降级而非错误。
    """
    ids = bot_registry.all_self_ids()
    return ids[0] if ids else None

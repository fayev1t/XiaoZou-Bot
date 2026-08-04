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

Program API functions all execute inside the current AgentLoop tick. There is
no ToolWorker, pending-tool notification, or tool-batch completion wake.
"""

from __future__ import annotations

import asyncio
from functools import partial
from typing import Any, Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from qqbot.core.logging import get_logger
from qqbot.services.agent_loop import bot_registry
from qqbot.services.agent_loop.decision import Planner
from qqbot.services.agent_loop.event_writer import (
    RuntimeEventPublisher,
    WakeMode,
)
from qqbot.services.agent_loop.loop import AgentLoop
from qqbot.services.agent_loop.projection import Projector
from qqbot.services.agent_loop.reply_executor import ReplyExecutor
from qqbot.services.agent_loop.silence_watcher import SilenceWatcher
from qqbot.services.agent_loop.tool_registry import ToolRegistry

logger = get_logger(__name__)

SessionFactory = Callable[[], AsyncSession]


class LoopSupervisor:
    def __init__(
        self,
        planner: Planner,
        session_factory: SessionFactory,
        projector: Projector | None = None,
        tool_registry: ToolRegistry | None = None,
        caption_image: Any | None = None,
    ) -> None:
        self._planner = planner
        self._session_factory = session_factory
        self._projector = projector
        self._tool_registry = tool_registry
        # 看图写描述回调（生产 = meme_caption.caption_image，由 v2_main 注入），
        # 原样转发给每个 AgentLoop 的 ProgramExecutor。
        self._caption_image = caption_image
        self._loops: dict[str, AgentLoop] = {}
        self._lock = asyncio.Lock()
        self._started = False
        self._stopped = False
        self._reply_executor: ReplyExecutor | None = None
        # 滚动记忆压缩器（记忆系统契约 §4）：MEMORY_COMPACTION_ENABLED
        # 打开时 start() 拉起；类型留 Any——模块惰性导入，避免默认关闭时
        # 平白拉进 LLM 依赖链。
        self._memory_compactor: Any | None = None
        # 静默叫醒（2026-08-03）：群里彻底安静满阈值时落一条事实事件并开一拍，
        # 给"回想"一个发生的时机。见 silence_watcher.py。
        self._silence_watcher: SilenceWatcher | None = None

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
        # ReplyExecutor 独立负责 reply_task 的到点生命周期完成（2026-07-31 起
        # 不再组稿发送）：completed 事件经其**专用** RuntimeEventPublisher 落库
        # 后，由这里注入的 wrapper 直接 immediate 唤醒——完成事实已落库、没有
        # 可攒的新消息，等攒批窗口只是白加延迟。publisher 协议本身不变。
        self._reply_executor = ReplyExecutor(
            session_factory=self._session_factory,
            event_publisher=RuntimeEventPublisher(
                self._session_factory,
                notify_event_available=self.waker(WakeMode.IMMEDIATE),
            ),
        )
        # rescan 与上面的任务回填同属恢复性动作，best-effort：失败只损失
        # "重挂定时器 / 补 uncertain / 补 wake"，不挡启动。
        try:
            await self._reply_executor.start()
        except Exception as exc:
            logger.warning(
                "[supervisor] reply executor rescan failed (continuing): {}",
                exc,
            )
        # MemoryCompactor（记忆系统契约 §4）：滚动折叠式场景记忆。开关
        # 默认关；启用时只挂起 worker 并给投影装推式探针。worker 启动不
        # 扫描、不 merge；只有 tick 投影报告真正触顶才会唤醒。best-effort：
        # 记忆压缩失败不能挡启动。
        try:
            from qqbot.services.agent_loop.memory_compactor import (
                MemoryCompactor,
                memory_compaction_enabled,
            )

            if memory_compaction_enabled():
                self._memory_compactor = MemoryCompactor(self._session_factory)
                self._memory_compactor.start()
                if self._projector is not None:
                    self._projector.set_uncovered_notifier(
                        self.notify_compaction
                    )
                logger.info("[supervisor] memory compactor online")
        except Exception as exc:
            logger.warning(
                "[supervisor] memory compactor start failed (continuing): {}",
                exc,
            )
        # 静默叫醒：构造即可，没有 worker 要拉起——计时器由 wake() 的活动通知
        # 按需武装。用 IMMEDIATE_NO_ARM：自己的叫醒不能重置自己的计时器，
        # 否则一段静默里会反复开拍。
        self._silence_watcher = SilenceWatcher(
            self._session_factory, self.waker(WakeMode.IMMEDIATE_NO_ARM)
        )
        if self._silence_watcher.enabled:
            logger.info("[supervisor] silence watcher online")
        # SystemAgentLoop wakes up to handle scope=system events
        # (request.*, lifecycle, bot_offline, ...).
        await self._ensure("system")
        self._started = True
        logger.info("[supervisor] started, system loop online")

    async def stop(self) -> None:
        self._stopped = True
        loops = list(self._loops.values())
        self._loops.clear()
        await asyncio.gather(
            *(loop.stop() for loop in loops), return_exceptions=True
        )
        if self._reply_executor is not None:
            await self._reply_executor.stop()
            self._reply_executor = None
        if self._silence_watcher is not None:
            try:
                await self._silence_watcher.stop()
            except Exception as exc:
                logger.warning("[supervisor] silence watcher stop failed: {}", exc)
            finally:
                self._silence_watcher = None
        if self._memory_compactor is not None:
            try:
                await self._memory_compactor.stop()
            except Exception as exc:
                logger.warning(
                    "[supervisor] memory compactor stop failed: {}", exc
                )
            finally:
                self._memory_compactor = None
        logger.info("[supervisor] stopped, {} loops drained", len(loops))

    def waker(self, mode: WakeMode) -> Callable[[str], Awaitable[None]]:
        """绑定 mode，返回朴素的 ``(scope_key) -> Awaitable[None]`` 回调。

        注入给生产者（ReplyExecutor / SilenceWatcher / `wait` 工具）的一律是
        这个形状：它们只表达"叫醒这个 scope"，用哪种模式是本处的装配决定。
        2026-08-04 用它取代了 `_wake_immediate` / `_wake_no_arm` 两个私有方法
        ——那两个私有方法本身就是被当回调注入出去的，等于三个入口三种形状。
        """
        return partial(self.wake, mode=mode)

    async def wake(
        self, scope_key: str, *, mode: WakeMode = WakeMode.BATCHED
    ) -> None:
        """唤醒某个 scope 的 loop。三种模式的语义见 ``WakeMode``。

        默认 BATCHED 走 AgentLoop 的攒批窗口（2026-07-28 引入，2026-08-01 改
        固定窗口）：新消息不立刻开拍，第一条开一个固定窗口，这段时间内到的一起
        在窗口到点那一拍看到，避免对着拆成几条发的半截话表态。
        """
        if self._stopped:
            return
        if scope_key.startswith("private:"):
            # 实例化策略 §10.1: private 不实例化 loop
            return
        # 有动静 → 重排静默计时器。放在 _ensure 之前：即便 loop 创建失败，
        # "这个 scope 刚才不静默"也是事实。IMMEDIATE_NO_ARM 跳过这一步，
        # 否则静默叫醒会重置自己的计时器、一段静默里反复开拍。
        if (
            mode is not WakeMode.IMMEDIATE_NO_ARM
            and self._silence_watcher is not None
        ):
            self._silence_watcher.notify_activity(scope_key)
        try:
            loop = await self._ensure(scope_key)
        except ValueError:
            logger.warning("[supervisor] invalid scope_key: {}", scope_key)
            return
        loop.wake(immediate=mode is not WakeMode.BATCHED)

    def notify_compaction(self, scope_key: str, uncovered_events: int) -> None:
        """转发投影计数；压缩器只接受达到阈值的 scope。

        未启用记忆压缩时 no-op。
        """
        if self._memory_compactor is not None:
            self._memory_compactor.notify(scope_key, uncovered_events)

    async def notify_reply_task(
        self,
        scope_key: str,
        reply_task_id: str,
        revision: int,
        flush_at: Any,
        event_id: str,
    ) -> None:
        if self._reply_executor is not None:
            await self._reply_executor.notify(
                scope_key, reply_task_id, revision, flush_at, event_id
            )

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
                caption_image=self._caption_image,
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
    <reply ... from_self="true"/> 的服务端标注识别"这条是回复我的"——这是降级而非错误。
    """
    ids = bot_registry.all_self_ids()
    return ids[0] if ids else None

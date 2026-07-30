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

工具批次与唤醒（2026-07-02 起无门闩）：ToolWorker 在整批 terminal + 写完
runtime.tool_batch_completed 后经 notify_tool_batch_completed **批次级唤醒
一次**（不是每个工具一次）。批次进行期间到达的其它 wake（新消息等）**不再
被推迟**——AgentLoop 随时开拍，模型自己看 timeline 里的
<tool-call status="processing"> 行决定等还是先处理新事件（prompt 教它不重拨）。
这是"模型+prompt 优先"哲学的落地：曾经的批次门闩（tool batch latch，上闩/
解闩/180s 超时兜底）是替弱模型防复读的程序级闸门，已随 pending_tool_results
一起拆除；防复读责任回归 prompt（§protocol tool batch 一节）。
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from qqbot.core.logging import get_logger
from qqbot.services.agent_loop import bot_registry
from qqbot.services.agent_loop.decision import Planner
from qqbot.services.agent_loop.event_writer import RuntimeEventPublisher
from qqbot.services.agent_loop.loop import AgentLoop
from qqbot.services.agent_loop.projection import Projector
from qqbot.services.agent_loop.reply_executor import ReplyExecutor
from qqbot.services.agent_loop.replyer import Replyer
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
        caption_image: Any | None = None,
        replyer: Replyer | None = None,
    ) -> None:
        self._planner = planner
        self._session_factory = session_factory
        self._projector = projector
        self._tool_registry = tool_registry
        # 看图写描述回调（生产 = meme_caption.caption_image，由 v2_main 注入）：
        # 原样转发给 ToolWorker，进工具 run() context 供 meme 工具用。
        self._caption_image = caption_image
        self._replyer = replyer
        self._loops: dict[str, AgentLoop] = {}
        self._lock = asyncio.Lock()
        self._started = False
        self._stopped = False
        self._tool_worker: ToolWorker | None = None
        self._reply_executor: ReplyExecutor | None = None
        # 滚动记忆压缩器（记忆系统契约 §4）：MEMORY_COMPACTION_ENABLED
        # 打开时 start() 拉起；类型留 Any——模块惰性导入，避免默认关闭时
        # 平白拉进 LLM 依赖链。
        self._memory_compactor: Any | None = None

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
        # ToolWorker：只有注入 registry 才启动；start() 即触发一次 catchup。把
        # 自己注入进去让 worker 在**整批工具收口后**（写完 runtime.
        # tool_batch_completed）经 notify_tool_batch_completed 批次级唤醒对应
        # scope 的 AgentLoop——不再是每 drain 一轮就按 scope wake 一次。
        # ReplyExecutor 独立负责 reply_task 的到点组稿与发送；它只向通用
        # RuntimeEventPublisher 发布事件，不直接持有 wake 接口。
        if self._tool_registry is not None:
            if self._projector is not None:
                self._reply_executor = ReplyExecutor(
                    session_factory=self._session_factory,
                    projector=self._projector,
                    event_publisher=RuntimeEventPublisher(
                        self._session_factory,
                        notify_event_available=self.wake,
                    ),
                    replyer=self._replyer,
                    # 与 AgentLoop 同一把 resolver：Replyer 的组稿 context 同样
                    # 带 bot_qq/bot_role（输入权重与 Planner 对齐，2026-07-22）。
                    bot_user_id_resolver=_default_bot_user_id_resolver,
                )
                # rescan 与上面的任务回填同属恢复性动作，best-effort：失败只
                # 损失"重挂定时器 / 补 uncertain / overdue hint"，模型侧仍有
                # timeline 上的 <tool-call name="reply"> 行作证据链可自愈
                # （再落一次稿即重新挂表），不挡启动。
                try:
                    await self._reply_executor.start()
                except Exception as exc:
                    logger.warning(
                        "[supervisor] reply executor rescan failed "
                        "(continuing): {}",
                        exc,
                    )
            self._tool_worker = ToolWorker(
                session_factory=self._session_factory,
                registry=self._tool_registry,
                supervisor=self,
                caption_image=self._caption_image,
            )
            self._tool_worker.start()
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
        # SystemAgentLoop wakes up to handle scope=system events
        # (request.*, lifecycle, bot_offline, ...).
        await self._ensure("system")
        self._started = True
        logger.info(
            "[supervisor] started, system loop + tool worker={} online",
            "yes" if self._tool_worker is not None else "no",
        )

    async def stop(self) -> None:
        self._stopped = True
        loops = list(self._loops.values())
        self._loops.clear()
        await asyncio.gather(
            *(loop.stop() for loop in loops), return_exceptions=True
        )
        if self._tool_worker is not None:
            try:
                await self._tool_worker.stop()
            except Exception as exc:
                logger.warning("[supervisor] tool_worker.stop failed: {}", exc)
            finally:
                self._tool_worker = None
        if self._reply_executor is not None:
            await self._reply_executor.stop()
            self._reply_executor = None
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

    async def wake(self, scope_key: str, *, immediate: bool = False) -> None:
        """唤醒某个 scope 的 loop。

        默认走 AgentLoop 的攒批窗口（2026-07-28）：新消息不立刻开拍，安静下来
        才开，避免对着拆成几条发的半截话表态。immediate=True 直接开拍，留给
        "活干完了，来看结果"类唤醒（工具批次收口）——那里没有可攒的东西。
        """
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
        loop.wake(immediate=immediate)

    def notify_tool_pending(self) -> None:
        """AgentLoop 写完 tool_called 后调，叫醒 ToolWorker 立即执行；未注入
        tool_registry 时是 no-op。"""
        if self._tool_worker is not None:
            self._tool_worker.notify()

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

    async def notify_tool_batch_completed(
        self, scope_key: str, tool_batch_id: str
    ) -> None:
        """ToolWorker 在 runtime.tool_batch_completed 落库后调用：批次级唤醒
        一次（不是每个工具一次——聚合唤醒是效率取舍，与"限制模型"无关）。

        2026-07-02 起没有批次门闩：这里只负责唤醒，不再有解闩/stale 匹配逻辑。
        批次进行期间的其它 wake 早已随时开拍。

        immediate=True（2026-07-28）：工具结果已经落库，模型正等着看，攒批窗口
        在这里没有任何东西可攒，等它就是白加延迟。
        """
        logger.info(
            "[supervisor] tool batch completed, waking scope={} batch={}",
            scope_key,
            tool_batch_id,
        )
        await self.wake(scope_key, immediate=True)

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

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
        # ReplyExecutor 独立负责 reply_task 的到点组稿与发送。
        if self._tool_registry is not None:
            if self._projector is not None:
                self._reply_executor = ReplyExecutor(
                    session_factory=self._session_factory,
                    projector=self._projector,
                    wake_scope=self.wake,
                    replyer=self._replyer,
                )
                # rescan 与上面的任务回填同属恢复性动作，best-effort：失败只
                # 损失"重挂定时器 / 补 uncertain / overdue hint"，模型侧仍有
                # <pending-reply> 证据链可自愈（合稿即重新挂表），不挡启动。
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

    def notify_tool_pending(self) -> None:
        """AgentLoop 写完 tool_called 后调，叫醒 ToolWorker 立即执行；未注入
        tool_registry 时是 no-op。"""
        if self._tool_worker is not None:
            self._tool_worker.notify()

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
        """
        logger.info(
            "[supervisor] tool batch completed, waking scope={} batch={}",
            scope_key,
            tool_batch_id,
        )
        await self.wake(scope_key)

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
    <reply ... from_self="true"/> 的服务端标注识别"这条是回复我的"——这是降级而非错误。
    """
    ids = bot_registry.all_self_ids()
    return ids[0] if ids else None

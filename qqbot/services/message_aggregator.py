"""消息聚合服务 - 动态对话响应块机制

核心概念：
- ResponseBlock（对话响应块）：收集一段时间内的连续消息
- 收到消息后，AI决定是立即回复还是等待更多消息
- 等待期间新消息继续加入块中
- 最终回复时，分析整块内容需要几次什么样的回复

这样可以：
1. 避免对连续消息做出多次重复回复
2. 更好地处理刷屏场景
3. 让AI有更完整的上下文来做决策
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

from qqbot.core.logging import get_logger, log_ai_input, log_ai_output, log_event
from qqbot.services.prompt import PromptManager

logger = get_logger(__name__)


@dataclass
class PendingMessage:
    """待处理的消息"""

    user_id: int
    raw_message: str
    formatted_message: str
    timestamp: float
    event: Any  # GroupMessageEvent
    is_bot_mentioned: bool = False


@dataclass
class ResponseBlock:
    """对话响应块 - 聚合一段时间内的消息"""

    group_id: int
    messages: list[PendingMessage] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    last_message_at: float = field(default_factory=time.time)
    is_processing: bool = False
    wait_task: asyncio.Task | None = None
    judge_wait_task: asyncio.Task | None = None  # 用于判断等待时间的API任务

    def add_message(self, msg: PendingMessage) -> None:
        """添加消息到块中"""
        self.messages.append(msg)
        self.last_message_at = time.time()

    def get_message_count(self) -> int:
        """获取消息数量"""
        return len(self.messages)

    def has_bot_mention(self) -> bool:
        """检查是否有@机器人的消息"""
        return any(msg.is_bot_mentioned for msg in self.messages)

    def get_unique_users(self) -> set[int]:
        """获取参与对话的用户ID集合"""
        return {msg.user_id for msg in self.messages}

    def clear(self) -> None:
        """清空块"""
        self.messages.clear()
        self.created_at = time.time()
        self.last_message_at = time.time()
        self.is_processing = False
        if self.wait_task and not self.wait_task.done():
            self.wait_task.cancel()
        self.wait_task = None
        if self.judge_wait_task and not self.judge_wait_task.done():
            self.judge_wait_task.cancel()
        self.judge_wait_task = None


class MessageAggregator:
    """消息聚合器 - 管理各群的对话响应块"""

    def __init__(self) -> None:
        """初始化聚合器"""
        # 每个群一个ResponseBlock
        self._blocks: dict[int, ResponseBlock] = {}
        # 锁，防止并发问题
        self._locks: dict[int, asyncio.Lock] = {}
        # 回调函数，用于触发回复处理
        self._reply_callback: Callable[
            [int, ResponseBlock], Coroutine[Any, Any, None]
        ] | None = None
        # 提示词管理器
        self.prompt_manager = PromptManager()

    def set_reply_callback(
        self,
        callback: Callable[[int, ResponseBlock], Coroutine[Any, Any, None]],
    ) -> None:
        """设置回复回调函数

        Args:
            callback: 异步回调函数，参数为 (group_id, block)
        """
        self._reply_callback = callback

    def _get_lock(self, group_id: int) -> asyncio.Lock:
        """获取群的锁"""
        if group_id not in self._locks:
            self._locks[group_id] = asyncio.Lock()
        return self._locks[group_id]

    def _get_block(self, group_id: int) -> ResponseBlock:
        """获取或创建群的ResponseBlock"""
        if group_id not in self._blocks:
            self._blocks[group_id] = ResponseBlock(group_id=group_id)
        return self._blocks[group_id]

    async def _judge_wait_time(self, group_id: int, block: ResponseBlock) -> None:
        """判断需要等待多长时间（3-20秒）

        基于块中的消息内容和历史上下文，API判断可能还有多少后续消息要来。
        如果这个任务被取消（新消息到达），会自动重新发送。

        Args:
            group_id: 群ID
            block: 对话响应块
        """
        try:
            # 获取历史上下文（从数据库）
            from qqbot.core.database import AsyncSessionLocal
            from qqbot.services.context import ContextManager
            from qqbot.services.silence_mode import is_silent

            history_context = ""
            silence_mode = is_silent(group_id)
            try:
                async with AsyncSessionLocal() as session:
                    context_manager = ContextManager(session)
                    history_context = await context_manager.get_recent_context(
                        group_id=group_id,
                        limit=20,  # Layer 0: 使用20条背景消息进行初筛判断
                        bot_id=block.messages[0].event.self_id if block.messages else None,
                    )
            except Exception as e:
                logger.warning(f"[aggregator] 获取历史上下文失败: {e}", extra={"group_id": group_id})
                history_context = ""

            # 格式化块中的消息
            block_content = "\n".join(
                msg.formatted_message for msg in block.messages
            )

            # 构建AI提示词（包含历史上下文和沉默模式状态）
            silence_hint = "\n【特殊状态：沉默模式激活】当前群处于沉默模式，只有在被@或有明确问题时才应判断为需要回复。其他闲聊一律判断为需要等待（should_wait=true）。" if silence_mode else ""
            prompt = f"""【历史上下文】（最近消息）
{history_context if history_context else "暂无历史上下文"}

【当前消息块】（刚接收到的消息，共{len(block.messages)}条）
{block_content}
{silence_hint}
{self.prompt_manager.wait_time_judge_prompt}"""

            from qqbot.core.llm import create_llm
            from langchain_core.messages import HumanMessage

            llm = await create_llm(temperature=0.5)

            # 记录 AI 输入（只记录消息块内容，不记录历史上下文和提示词）
            log_input = f"【消息块】共{len(block.messages)}条\n{block_content}"
            if silence_mode:
                log_input += "\n【沉默模式】已激活"
            log_ai_input("Layer0", group_id, log_input)

            response = await llm.ainvoke([
                HumanMessage(content=prompt)
            ])

            # 解析响应
            import json
            response_text = response.content.strip()
            
            # 记录 AI 输出
            log_ai_output("Layer0", group_id, response_text)
            json_start = response_text.find("{")
            json_end = response_text.rfind("}") + 1

            if json_start >= 0 and json_end > json_start:
                json_str = response_text[json_start:json_end]
                result = json.loads(json_str)

                should_wait = result.get("should_wait", True)

                if should_wait:
                    # 需要等待，获取等待时间
                    wait_time = result.get("wait_seconds")
                    if wait_time is None:
                        logger.warning(f"[aggregator] should_wait=true 但缺少 wait_seconds，使用默认5秒", extra={"group_id": group_id})
                        wait_time = 5.0
                    else:
                        # 确保在 3-10 秒范围内（取保值）
                        wait_time = max(3.0, min(10.0, float(wait_time)))

                    msg = f"[aggregator] 🤖 AI判断：等待 {wait_time}秒 (原因: {result.get('reason', '无')})"
                    logger.info(msg, extra={"group_id": group_id, "wait_seconds": wait_time, "should_wait": True})

                    # 启动实际的等待任务
                    block.wait_task = asyncio.create_task(
                        self._wait_and_process(group_id, wait_time)
                    )
                else:
                    # 不需要等待，立即处理
                    msg = f"[aggregator] 🤖 AI判断：不等待，立即处理 (原因: {result.get('reason', '无')})"
                    logger.info(msg, extra={"group_id": group_id, "should_wait": False})

                    # 立即启动处理（0秒等待）
                    block.wait_task = asyncio.create_task(
                        self._wait_and_process(group_id, 0.0)
                    )
            else:
                # 解析失败，使用默认等待5秒
                msg = f"[aggregator] ⚠️ JSON解析失败，使用默认等待5秒"
                logger.warning(msg, extra={"group_id": group_id})
                block.wait_task = asyncio.create_task(
                    self._wait_and_process(group_id, 5.0)
                )

        except asyncio.CancelledError:
            # 任务被取消（新消息到达），直接返回，会由add_message重新启动
            msg = f"[aggregator] ↩️ 等待时间判断被取消（检测到新消息） | 群={group_id}"
            logger.debug(msg)
        except Exception as e:
            logger.warning(f"[aggregator] 判断等待时间失败: {e}, 使用默认2秒", extra={"group_id": group_id})
            block.wait_task = asyncio.create_task(
                self._wait_and_process(group_id, 2.0)
            )

    async def add_message(
        self,
        group_id: int,
        user_id: int,
        raw_message: str,
        formatted_message: str,
        event: Any,
        is_bot_mentioned: bool = False,
    ) -> None:
        """添加消息到聚合块

        消息添加后，如果当前没有等待任务，会启动一个等待任务。
        等待期间如果有新消息，会重置等待计时器。
        等待结束后触发回复处理。

        Args:
            group_id: 群ID
            user_id: 用户ID
            raw_message: 原始消息内容
            formatted_message: System-XML 格式内容
            event: 原始事件对象
            is_bot_mentioned: 是否@了机器人
        """
        lock = self._get_lock(group_id)

        async with lock:
            block = self._get_block(group_id)

            # 如果正在处理中，创建新块让新消息进入（不中断旧块的处理）
            if block.is_processing:
                msg = f"[aggregator] ⏹️ 旧块正在处理中，新消息创建新块 | 群={group_id}"
                logger.debug(msg, extra={"group_id": group_id})
                # 创建全新的块
                self._blocks[group_id] = ResponseBlock(group_id=group_id)
                block = self._blocks[group_id]

            # 创建待处理消息
            pending_msg = PendingMessage(
                user_id=user_id,
                raw_message=raw_message,
                formatted_message=formatted_message,
                timestamp=time.time(),
                event=event,
                is_bot_mentioned=is_bot_mentioned,
            )

            # 检查是否是新块
            is_new_block = block.get_message_count() == 0

            # 添加到块中
            block.add_message(pending_msg)

            if is_new_block:
                log_event("block_created", group_id=group_id)
                msg = f"[aggregator] 📦 创建新对话块: 群{group_id}"
                logger.info(msg, extra={"group_id": group_id})

            msg = (
                f"[aggregator] ➕ 消息已加入块 | 群={group_id}, "
                f"消息数={block.get_message_count()}, "
                f"用户数={len(block.get_unique_users())}, "
                f"@机器人={block.has_bot_mention()}, "
                f"内容={formatted_message[:30]}"
            )
            logger.info(msg, extra={
                "group_id": group_id,
                "message_count": block.get_message_count(),
                "unique_users": len(block.get_unique_users()),
                "has_bot_mention": block.has_bot_mention(),
                "is_bot_mentioned": is_bot_mentioned,
                "user_id": user_id,
            })

            # 如果已有等待任务，取消它（因为有新消息，需要重新评估）
            if block.wait_task and not block.wait_task.done():
                msg = f"[aggregator] ⏸️ 取消旧等待任务 | 群={group_id}"
                logger.debug(msg, extra={"group_id": group_id})
                block.wait_task.cancel()
                try:
                    await block.wait_task
                except asyncio.CancelledError:
                    pass

            # 如果等待时间判断API还在处理中，也要取消
            if block.judge_wait_task and not block.judge_wait_task.done():
                msg = f"[aggregator] 🔄 取消旧的等待时间判断API，重新发送 | 群={group_id}"
                logger.debug(msg, extra={"group_id": group_id})
                block.judge_wait_task.cancel()
                try:
                    await block.judge_wait_task
                except asyncio.CancelledError:
                    pass
            # 启动新的等待时间判断API调用
            block.judge_wait_task = asyncio.create_task(
                self._judge_wait_time(group_id, block)
            )

            msg = f"[aggregator] 📡 发送等待时间判断API | 群={group_id}"
            logger.debug(msg, extra={"group_id": group_id})

    async def _wait_and_process(self, group_id: int, wait_seconds: float) -> None:
        """等待一段时间后处理块

        Args:
            group_id: 群ID
            wait_seconds: 等待时间（秒）
        """
        try:
            # 等待指定时间
            await asyncio.sleep(wait_seconds)

            lock = self._get_lock(group_id)
            async with lock:
                block = self._get_block(group_id)

                # 再次检查是否有消息需要处理
                if block.get_message_count() == 0:
                    logger.debug(f"[aggregator] Block for group {group_id} is empty, skipping")
                    return

                # 标记为处理中
                block.is_processing = True
                processing_block = block

                msg = f"[aggregator] ⏰ 等待时间已到，对话块已关闭 | 群={group_id}, 块内消息数={block.get_message_count()}, 用户数={len(block.get_unique_users())}, @机器人={block.has_bot_mention()}"
                logger.info(msg, extra={
                    "group_id": group_id,
                    "message_count": block.get_message_count(),
                    "unique_users": len(block.get_unique_users()),
                    "has_bot_mention": block.has_bot_mention(),
                })

            # 触发回复处理（在锁外执行，避免阻塞新消息）
            if self._reply_callback:
                try:
                    msg = f"[aggregator] 🔷 触发回复处理回调 | 群={group_id}"
                    logger.debug(msg, extra={"group_id": group_id})
                    await self._reply_callback(group_id, processing_block)
                except Exception as e:
                    logger.error(
                        f"[aggregator] Reply callback failed for group {group_id}: {e}",
                        exc_info=True,
                    )

            # 处理完成后清空块
            async with lock:
                current_block = self._blocks.get(group_id)
                if current_block is processing_block:
                    msg = f"[aggregator] 🧹 对话块已处理完毕，清空块 | 群={group_id}"
                    logger.info(msg, extra={"group_id": group_id})
                else:
                    logger.debug(
                        "[aggregator] New block exists; cleaning processed block only",
                        extra={"group_id": group_id},
                    )
                processing_block.clear()

        except asyncio.CancelledError:
            # 任务被取消（因为有新消息），这是正常的
            msg = f"[aggregator] ↩️ 等待任务被取消（检测到新消息） | 群={group_id}"
            logger.debug(msg)

    def get_block_info(self, group_id: int) -> dict[str, Any]:
        """获取块的状态信息（用于调试）

        Args:
            group_id: 群ID

        Returns:
            块的状态信息字典
        """
        if group_id not in self._blocks:
            return {"exists": False}

        block = self._blocks[group_id]
        return {
            "exists": True,
            "message_count": block.get_message_count(),
            "is_processing": block.is_processing,
            "has_wait_task": block.wait_task is not None,
            "unique_users": list(block.get_unique_users()),
            "has_bot_mention": block.has_bot_mention(),
            "age_seconds": time.time() - block.created_at,
        }


# 全局单例
message_aggregator = MessageAggregator()

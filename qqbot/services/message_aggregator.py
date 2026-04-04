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
import importlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

from qqbot.core.logging import get_logger, log_ai_input, log_ai_output, log_event
from qqbot.services.prompt import PromptManager

logger = get_logger(__name__)


def _build_parse_failed_placeholder(reason: str = "消息未完成格式化") -> str:
    normalized_reason = reason.strip() if reason else "消息不可用"
    return f"【解析失败：{normalized_reason}】"


PARSE_FAILED_PLACEHOLDER = _build_parse_failed_placeholder()

PRE_CLOSE_QUIET_SECONDS = 3.5


@dataclass
class PendingMessage:
    """待处理的消息"""

    user_id: int
    msg_hash: str
    raw_message: str
    formatted_message: str | None = None
    timestamp: float = field(default_factory=time.time)
    event: Any = None
    is_bot_mentioned: bool = False
    format_task: asyncio.Task | None = None
    persisted_message_id: int | None = None


@dataclass
class ResponseBlock:
    """对话响应块 - 聚合一段时间内的消息"""

    group_id: int
    messages: list[PendingMessage] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    last_message_at: float = field(default_factory=time.time)
    is_processing: bool = False
    wait_task: asyncio.Task | None = None
    judge_wait_task: asyncio.Task | None = None
    judge_request_version: int = 0

    def add_message(self, msg: PendingMessage) -> None:
        self.messages.append(msg)
        self.last_message_at = time.time()

    def get_message_count(self) -> int:
        return len(self.messages)

    def has_bot_mention(self) -> bool:
        return any(msg.is_bot_mentioned for msg in self.messages)

    def get_unique_users(self) -> set[int]:
        return {msg.user_id for msg in self.messages}

    def get_earliest_persisted_message_id(self) -> int | None:
        message_ids = [
            msg.persisted_message_id
            for msg in self.messages
            if msg.persisted_message_id is not None
        ]
        if not message_ids:
            return None
        return min(message_ids)

    def clear(self) -> None:
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
        self.judge_request_version = 0


class MessageAggregator:
    """消息聚合器 - 管理各群的对话响应块"""

    def __init__(self) -> None:
        self._blocks: dict[int, ResponseBlock] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._group_versions: dict[int, int] = {}
        self._pending_persist_counts: dict[int, int] = {}
        self._pre_close_quiet_seconds = PRE_CLOSE_QUIET_SECONDS
        self._reply_callback: Callable[
            [int, ResponseBlock], Coroutine[Any, Any, None]
        ] | None = None
        self.prompt_manager = PromptManager()

    def set_reply_callback(
        self,
        callback: Callable[[int, ResponseBlock], Coroutine[Any, Any, None]],
    ) -> None:
        self._reply_callback = callback

    def _get_lock(self, group_id: int) -> asyncio.Lock:
        if group_id not in self._locks:
            self._locks[group_id] = asyncio.Lock()
        return self._locks[group_id]

    def _get_block(self, group_id: int) -> ResponseBlock:
        if group_id not in self._blocks:
            self._blocks[group_id] = ResponseBlock(group_id=group_id)
        return self._blocks[group_id]

    def _get_group_version(self, group_id: int) -> int:
        return self._group_versions.get(group_id, 0)

    def _bump_group_version(self, group_id: int) -> int:
        next_version = self._get_group_version(group_id) + 1
        self._group_versions[group_id] = next_version
        return next_version

    def _get_pending_persist_count(self, group_id: int) -> int:
        return self._pending_persist_counts.get(group_id, 0)

    def _cancel_active_tasks(self, block: ResponseBlock) -> None:
        for task in (block.wait_task, block.judge_wait_task):
            if task and not task.done():
                task.cancel()
        block.wait_task = None
        block.judge_wait_task = None

    def _is_snapshot_current(
        self,
        group_id: int,
        block: ResponseBlock,
        expected_version: int,
        *,
        require_no_pending: bool,
        expected_judge_request_version: int | None = None,
    ) -> bool:
        current_block = self._blocks.get(group_id)
        if current_block is not block:
            return False
        if self._get_group_version(group_id) != expected_version:
            return False
        if (
            expected_judge_request_version is not None
            and block.judge_request_version != expected_judge_request_version
        ):
            return False
        if require_no_pending and self._get_pending_persist_count(group_id) > 0:
            return False
        return True

    def _schedule_judge_task(self, group_id: int, block: ResponseBlock) -> None:
        if block.is_processing or block.get_message_count() == 0:
            return
        if self._get_pending_persist_count(group_id) > 0:
            return
        expected_version = self._get_group_version(group_id)
        block.judge_request_version += 1
        expected_judge_request_version = block.judge_request_version
        block.judge_wait_task = asyncio.create_task(
            self._judge_wait_time(
                group_id,
                block,
                expected_version,
                expected_judge_request_version,
            )
        )

    def _schedule_wait_task(
        self,
        group_id: int,
        block: ResponseBlock,
        expected_version: int,
        wait_seconds: float,
        expected_judge_request_version: int | None = None,
    ) -> None:
        if not self._is_snapshot_current(
            group_id,
            block,
            expected_version,
            require_no_pending=True,
            expected_judge_request_version=expected_judge_request_version,
        ):
            return
        block.wait_task = asyncio.create_task(
            self._wait_and_process(group_id, block, expected_version, wait_seconds)
        )

    async def begin_message_persist(self, group_id: int) -> None:
        lock = self._get_lock(group_id)
        async with lock:
            block = self._get_block(group_id)
            self._pending_persist_counts[group_id] = (
                self._get_pending_persist_count(group_id) + 1
            )
            self._bump_group_version(group_id)
            self._cancel_active_tasks(block)

    async def complete_message_persist(self, group_id: int) -> None:
        lock = self._get_lock(group_id)
        async with lock:
            self._pending_persist_counts[group_id] = max(
                0,
                self._get_pending_persist_count(group_id) - 1,
            )
            block = self._get_block(group_id)
            if self._get_pending_persist_count(group_id) == 0:
                self._schedule_judge_task(group_id, block)

    async def fail_message_persist(self, group_id: int) -> None:
        await self.complete_message_persist(group_id)

    async def finish_message_persist_and_add_message(
        self,
        group_id: int,
        user_id: int,
        msg_hash: str,
        raw_message: str,
        formatted_message: str | None,
        format_task: asyncio.Task | None,
        event: Any,
        persisted_message_id: int | None = None,
        is_bot_mentioned: bool = False,
    ) -> None:
        lock = self._get_lock(group_id)
        async with lock:
            block = self._get_block(group_id)
            if block.is_processing:
                logger.debug(
                    "[aggregator] ⏹️ 旧块正在处理中，新消息创建新块 | 群={} ",
                    group_id,
                    extra={"group_id": group_id},
                )
                self._blocks[group_id] = ResponseBlock(group_id=group_id)
                block = self._blocks[group_id]

            self._pending_persist_counts[group_id] = max(
                0,
                self._get_pending_persist_count(group_id) - 1,
            )

            pending_msg = PendingMessage(
                user_id=user_id,
                msg_hash=msg_hash,
                raw_message=raw_message,
                formatted_message=formatted_message,
                event=event,
                is_bot_mentioned=is_bot_mentioned,
                format_task=format_task,
                persisted_message_id=persisted_message_id,
            )

            is_new_block = block.get_message_count() == 0
            block.add_message(pending_msg)
            self._bump_group_version(group_id)
            self._cancel_active_tasks(block)

            if is_new_block:
                log_event("block_created", group_id=group_id)
                logger.info(
                    f"[aggregator] 📦 创建新对话块: 群{group_id}",
                    extra={"group_id": group_id},
                )

            preview_text = raw_message or formatted_message or PARSE_FAILED_PLACEHOLDER
            logger.info(
                f"[aggregator] ➕ 消息已加入块 | 群={group_id}, 消息数={block.get_message_count()}, 用户数={len(block.get_unique_users())}, @机器人={block.has_bot_mention()}, 内容={preview_text[:30]}",
                extra={
                    "group_id": group_id,
                    "message_count": block.get_message_count(),
                    "unique_users": len(block.get_unique_users()),
                    "has_bot_mention": block.has_bot_mention(),
                    "is_bot_mentioned": is_bot_mentioned,
                    "user_id": user_id,
                    "pending_persist_count": self._get_pending_persist_count(group_id),
                    "group_version": self._get_group_version(group_id),
                },
            )

            self._schedule_judge_task(group_id, block)
            logger.debug(
                f"[aggregator] 📡 发送等待时间判断API | 群={group_id}",
                extra={"group_id": group_id},
            )

    async def _judge_wait_time(
        self,
        group_id: int,
        block: ResponseBlock,
        expected_version: int,
        expected_judge_request_version: int,
    ) -> None:
        try:
            from qqbot.services.silence_mode import is_silent
            from qqbot.core.llm import create_llm
            messages_module = importlib.import_module("langchain_core.messages")
            human_message_class = messages_module.HumanMessage

            silence_mode = is_silent(group_id)

            lock = self._get_lock(group_id)
            async with lock:
                if not self._is_snapshot_current(
                    group_id,
                    block,
                    expected_version,
                    require_no_pending=True,
                    expected_judge_request_version=expected_judge_request_version,
                ):
                    return

            block_content = "\n".join(msg.raw_message for msg in block.messages)
            silence_hint = "\n【特殊状态：沉默模式激活】当前群处于沉默模式，只有在被@或有明确问题时才应判断为需要回复。其他闲聊一律判断为需要等待（should_wait=true）。" if silence_mode else ""
            prompt = f"""【当前消息块】（刚接收到的原始消息，共{len(block.messages)}条）
{block_content}
{silence_hint}
{self.prompt_manager.wait_time_judge_prompt}"""

            llm = await create_llm(temperature=0.5)
            if llm is None:
                logger.warning(
                    "[aggregator] LLM unavailable, using default wait time",
                    extra={"group_id": group_id},
                )
                async with lock:
                    self._schedule_wait_task(
                        group_id,
                        block,
                        expected_version,
                        5.0,
                        expected_judge_request_version,
                    )
                return

            log_input = f"【消息块】共{len(block.messages)}条\n{block_content}"
            if silence_mode:
                log_input += "\n【沉默模式】已激活"
            log_ai_input("Layer1", group_id, log_input)

            response = await llm.ainvoke([human_message_class(content=prompt)])

            async with lock:
                if not self._is_snapshot_current(
                    group_id,
                    block,
                    expected_version,
                    require_no_pending=True,
                    expected_judge_request_version=expected_judge_request_version,
                ):
                    return

            response_text = response.content.strip()
            log_ai_output("Layer1", group_id, response_text)
            json_start = response_text.find("{")
            json_end = response_text.rfind("}") + 1

            if json_start < 0 or json_end <= json_start:
                logger.warning(
                    f"[aggregator] ⚠️ JSON解析失败，使用默认等待5秒",
                    extra={"group_id": group_id},
                )
                async with lock:
                    self._schedule_wait_task(
                        group_id,
                        block,
                        expected_version,
                        5.0,
                        expected_judge_request_version,
                    )
                return

            result = json.loads(response_text[json_start:json_end])
            should_wait = result.get("should_wait", True)

            if should_wait:
                wait_time = result.get("wait_seconds")
                if wait_time is None:
                    logger.warning(
                        f"[aggregator] should_wait=true 但缺少 wait_seconds，使用默认5秒",
                        extra={"group_id": group_id},
                    )
                    wait_time = 5.0
                else:
                    wait_time = max(3.0, min(10.0, float(wait_time)))

                logger.info(
                    f"[aggregator] 🤖 AI判断：等待 {wait_time}秒 (原因: {result.get('reason', '无')})",
                    extra={
                        "group_id": group_id,
                        "wait_seconds": wait_time,
                        "should_wait": True,
                    },
                )
                async with lock:
                    self._schedule_wait_task(
                        group_id,
                        block,
                        expected_version,
                        wait_time,
                        expected_judge_request_version,
                    )
                return

            logger.info(
                f"[aggregator] 🤖 AI判断：不等待，立即处理 (原因: {result.get('reason', '无')})",
                extra={"group_id": group_id, "should_wait": False},
            )
            async with lock:
                self._schedule_wait_task(
                    group_id,
                    block,
                    expected_version,
                    0.0,
                    expected_judge_request_version,
                )

        except asyncio.CancelledError:
            logger.debug(
                f"[aggregator] ↩️ 等待时间判断被取消（检测到新消息） | 群={group_id}"
            )
        except Exception as e:
            logger.warning(
                "[aggregator] 判断等待时间失败: {!r}, 使用默认2秒",
                e,
                extra={"group_id": group_id},
            )
            async with self._get_lock(group_id):
                self._schedule_wait_task(
                    group_id,
                    block,
                    expected_version,
                    2.0,
                    expected_judge_request_version,
                )

    async def _wait_and_process(
        self,
        group_id: int,
        block: ResponseBlock,
        expected_version: int,
        wait_seconds: float,
    ) -> None:
        try:
            await asyncio.sleep(wait_seconds)

            lock = self._get_lock(group_id)
            format_tasks: list[asyncio.Task] = []
            async with lock:
                if not self._is_snapshot_current(
                    group_id,
                    block,
                    expected_version,
                    require_no_pending=True,
                ):
                    return
                if block.get_message_count() == 0:
                    logger.debug(
                        f"[aggregator] Block for group {group_id} is empty, skipping"
                    )
                    return

                format_tasks = [
                    pending_message.format_task
                    for pending_message in block.messages
                    if pending_message.format_task is not None
                ]

                logger.info(
                    f"[aggregator] ⏳ 第一层关闭条件已满足，进入冻结前静默窗口 | 群={group_id}, quiet={self._pre_close_quiet_seconds}秒, 块内消息数={block.get_message_count()}, 用户数={len(block.get_unique_users())}, @机器人={block.has_bot_mention()}",
                    extra={
                        "group_id": group_id,
                        "message_count": block.get_message_count(),
                        "unique_users": len(block.get_unique_users()),
                        "has_bot_mention": block.has_bot_mention(),
                        "group_version": expected_version,
                        "wait_seconds": wait_seconds,
                        "pre_close_quiet_seconds": self._pre_close_quiet_seconds,
                        "format_task_count": len(format_tasks),
                    },
                )

            if format_tasks:
                results = await asyncio.gather(
                    *[asyncio.shield(task) for task in format_tasks],
                    return_exceptions=True,
                )
                failed_task_count = sum(
                    1 for result in results if isinstance(result, Exception)
                )
                if failed_task_count > 0:
                    logger.warning(
                        "[aggregator] format tasks finished with failures before quiet window",
                        extra={
                            "group_id": group_id,
                            "failed_task_count": failed_task_count,
                            "format_task_count": len(format_tasks),
                        },
                    )

            await asyncio.sleep(self._pre_close_quiet_seconds)

            async with lock:
                if not self._is_snapshot_current(
                    group_id,
                    block,
                    expected_version,
                    require_no_pending=True,
                ):
                    return
                if block.get_message_count() == 0:
                    logger.debug(
                        f"[aggregator] Block for group {group_id} is empty, skipping"
                    )
                    return

                block.is_processing = True
                block.wait_task = None
                processing_block = block

                logger.info(
                    f"[aggregator] ⏰ 冻结前静默窗口结束，对话块已关闭 | 群={group_id}, 块内消息数={block.get_message_count()}, 用户数={len(block.get_unique_users())}, @机器人={block.has_bot_mention()}",
                    extra={
                        "group_id": group_id,
                        "message_count": block.get_message_count(),
                        "unique_users": len(block.get_unique_users()),
                        "has_bot_mention": block.has_bot_mention(),
                        "group_version": expected_version,
                        "wait_seconds": wait_seconds,
                        "pre_close_quiet_seconds": self._pre_close_quiet_seconds,
                    },
                )

            if self._reply_callback:
                try:
                    logger.debug(
                        f"[aggregator] 🔷 触发回复处理回调 | 群={group_id}",
                        extra={"group_id": group_id},
                    )
                    await self._reply_callback(group_id, processing_block)
                except Exception as e:
                    logger.error(
                        "[aggregator] Reply callback failed for group {}: {!r}",
                        group_id,
                        e,
                        exc_info=True,
                    )

            async with lock:
                current_block = self._blocks.get(group_id)
                if current_block is processing_block:
                    logger.info(
                        f"[aggregator] 🧹 对话块已处理完毕，清空块 | 群={group_id}",
                        extra={"group_id": group_id},
                    )
                else:
                    logger.debug(
                        "[aggregator] New block exists; cleaning processed block only",
                        extra={"group_id": group_id},
                    )
                processing_block.clear()

        except asyncio.CancelledError:
            logger.debug(
                f"[aggregator] ↩️ 等待任务被取消（检测到新消息） | 群={group_id}"
            )

    def get_block_info(self, group_id: int) -> dict[str, Any]:
        if group_id not in self._blocks:
            return {"exists": False}

        block = self._blocks[group_id]
        return {
            "exists": True,
            "message_count": block.get_message_count(),
            "is_processing": block.is_processing,
            "has_wait_task": block.wait_task is not None,
            "has_judge_wait_task": block.judge_wait_task is not None,
            "pending_persist_count": self._get_pending_persist_count(group_id),
            "group_version": self._get_group_version(group_id),
            "unique_users": list(block.get_unique_users()),
            "has_bot_mention": block.has_bot_mention(),
            "age_seconds": time.time() - block.created_at,
        }

    async def shutdown(self) -> None:
        pending_tasks: list[asyncio.Task] = []
        for block in self._blocks.values():
            for task in (block.wait_task, block.judge_wait_task):
                if task and not task.done():
                    task.cancel()
                    pending_tasks.append(task)

        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)

        for block in self._blocks.values():
            block.clear()

        self._blocks.clear()
        self._locks.clear()
        self._group_versions.clear()
        self._pending_persist_counts.clear()


message_aggregator = MessageAggregator()

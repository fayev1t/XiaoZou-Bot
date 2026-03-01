"""对话块判断服务 - 分析聚合的消息块，决定回复策略

替代原来的单消息判断（message_judge.py），改为对整个对话块进行判断。
"""

import json
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from sqlalchemy.ext.asyncio import AsyncSession

from qqbot.core.llm import LLMConfig
from qqbot.core.logging import get_logger, log_ai_input, log_ai_output, log_event
from qqbot.services.message_aggregator import ResponseBlock
from qqbot.services.prompt import PromptManager
from qqbot.services.silence_mode import is_silent, set_silent

logger = get_logger(__name__)


class JudgeResult:
    """Result of message judgment analysis (legacy compatibility class).

    Used to maintain compatibility with ConversationService which expects
    this format. New code should use BlockJudgeResult instead.
    """

    def __init__(
        self,
        should_reply: bool,
        reply_type: str,
        target_user_id: int | None = None,
        emotion: str = "happy",
        explanation: str = "",
        instruction: str = "",
        should_mention: bool = False,
        user_complaining_too_much: bool = False,
        user_asking_to_speak: bool = False,
    ) -> None:
        """Initialize judgment result.

        Args:
            should_reply: Whether bot should reply to this message
            reply_type: Type of reply - "person", "topic", "knowledge", or "none"
            target_user_id: If replying to a person, their QQ ID
            emotion: Emotion for the response - happy/serious/sarcastic/confused/gentle
            explanation: Explanation of the judgment decision
            instruction: Instructions for the response generation layer
            should_mention: Whether to @ mention the target user (only in special cases)
            user_complaining_too_much: User is complaining bot talks too much
            user_asking_to_speak: User is asking bot to speak more
        """
        self.should_reply = should_reply
        self.reply_type = reply_type
        self.target_user_id = target_user_id
        self.emotion = emotion
        self.explanation = explanation
        self.instruction = instruction
        self.should_mention = should_mention
        self.user_complaining_too_much = user_complaining_too_much
        self.user_asking_to_speak = user_asking_to_speak

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JudgeResult":
        """Create JudgeResult from dictionary.

        Args:
            data: Dictionary with judgment result fields

        Returns:
            JudgeResult instance
        """
        return cls(
            should_reply=data.get("should_reply", False),
            reply_type=data.get("reply_type", "none"),
            target_user_id=data.get("target_user_id"),
            emotion=data.get("emotion", "happy"),
            explanation=data.get("explanation", ""),
            instruction=data.get("instruction", ""),
            should_mention=data.get("should_mention", False),
            user_complaining_too_much=data.get("user_complaining_too_much", False),
            user_asking_to_speak=data.get("user_asking_to_speak", False),
        )


@dataclass
class ReplyPlan:
    """单次回复的计划"""

    target_user_id: int | None = None
    emotion: str = "happy"
    instruction: str = ""
    should_mention: bool = False
    related_messages: str = ""


@dataclass
class BlockJudgeResult:
    """对话块判断结果"""

    should_reply: bool
    reply_count: int
    block_summary: str
    replies: list[ReplyPlan] = field(default_factory=list)
    explanation: str = ""
    should_enter_silence_mode: bool = False
    should_exit_silence_mode: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BlockJudgeResult":
        """从字典创建结果对象

        Args:
            data: 包含判断结果的字典

        Returns:
            BlockJudgeResult实例
        """
        replies = []
        for r in data.get("replies", []):
            replies.append(
                ReplyPlan(
                    target_user_id=r.get("target_user_id"),
                    emotion=r.get("emotion", "happy"),
                    instruction=r.get("instruction", ""),
                    should_mention=r.get("should_mention", False),
                    related_messages=r.get("related_messages", ""),
                )
            )

        return cls(
            should_reply=data.get("should_reply", False),
            reply_count=data.get("reply_count", 0),
            block_summary=data.get("block_summary", ""),
            replies=replies,
            explanation=data.get("explanation", ""),
            should_enter_silence_mode=data.get("should_enter_silence_mode", False),
            should_exit_silence_mode=data.get("should_exit_silence_mode", False),
        )

    @classmethod
    def no_reply(cls, reason: str = "") -> "BlockJudgeResult":
        """创建一个不回复的结果

        Args:
            reason: 不回复的原因

        Returns:
            表示不回复的BlockJudgeResult
        """
        return cls(
            should_reply=False,
            reply_count=0,
            block_summary="",
            replies=[],
            explanation=reason,
        )


class BlockJudger:
    """对话块判断服务 - 分析整个对话块决定回复策略"""

    def __init__(self) -> None:
        """初始化判断服务"""
        self.config = LLMConfig()
        self.prompt_manager = PromptManager()
        self._llm = None

    async def _get_llm(self) -> Any:
        """获取或创建LLM实例

        Returns:
            LLM实例
        """
        if self._llm is None:
            from qqbot.core.llm import create_llm
            self._llm = await create_llm(temperature=0.5)
        return self._llm

    def _format_block_messages(
        self,
        block: ResponseBlock,
        user_names: dict[int, str] | None = None,
    ) -> str:
        """格式化对话块中的消息

        Args:
            block: 对话响应块
            user_names: 用户ID到昵称的映射

        Returns:
            格式化的消息文本
        """
        _ = user_names
        lines = []

        for msg in block.messages:
            lines.append(msg.formatted_message)

        return "\n".join(lines)

    async def judge_block(
        self,
        block: ResponseBlock,
        context: str,
        group_id: int | None = None,
        user_names: dict[int, str] | None = None,
    ) -> BlockJudgeResult:
        """判断对话块是否需要回复，以及如何回复

        Args:
            block: 对话响应块
            context: 历史上下文（从数据库获取的最近消息）
            group_id: 群ID（用于沉默模式管理）
            user_names: 用户ID到昵称的映射

        Returns:
            BlockJudgeResult包含回复策略
        """
        try:
            # 检查块是否为空
            if block.get_message_count() == 0:
                return BlockJudgeResult.no_reply("对话块为空")

            llm = await self._get_llm()

            # 检查沉默模式
            silence_mode = is_silent(group_id) if group_id else False

            # 格式化对话块消息
            block_content = self._format_block_messages(block, user_names)

            # 构建用户提示
            user_prompt = f"""【历史上下文】
{context}

【当前对话块】（{block.get_message_count()}条消息，来自{len(block.get_unique_users())}个用户）
{block_content}

请分析这个对话块并判断：
1. 是否需要回复？
2. 如果需要回复，需要回复几次？（通常1次就够）
3. 每次回复针对什么内容，用什么情绪？
4. 用户是否在抱怨AI说话太多？
5. 用户是否在催促AI说话？

输出JSON格式的判断结果。"""

            # 获取系统提示词
            system_prompt = self.prompt_manager.block_judge_prompt

            # 如果处于沉默模式，添加额外说明
            if silence_mode:
                system_prompt += "\n\n【特殊状态：沉默模式激活】当前群处于沉默模式，回复的判定标准应该变得严格。在这个模式下，只有在以下情况才应该判定为需要回复：用户明确@你、用户提出知识型问题、或有重要信息需要传达。其他的闲聊、话题讨论等应该判定为\'不回复\'。"

            logger.info(
                f"[block_judge] Judging block with {block.get_message_count()} messages",
                extra={
                    "group_id": group_id,
                    "message_count": block.get_message_count(),
                    "unique_users": len(block.get_unique_users()),
                    "silence_mode": silence_mode,
                },
            )

            # 调用LLM
            messages = [
                SystemMessage(content=system_prompt),
                HumanMessage(content=user_prompt),
            ]

            # 记录 AI 输入（只记录消息块内容，不记录历史上下文和提示词）
            log_input = f"【消息块】{block.get_message_count()}条消息，{len(block.get_unique_users())}个用户\n{block_content}"
            if silence_mode:
                log_input += "\n【沉默模式】已激活"
            log_ai_input("Layer1", group_id or 0, log_input)

            response = await llm.ainvoke(messages)
            response_text = response.content.strip()

            # 记录 AI 输出
            log_ai_output("Layer1", group_id or 0, response_text)

            # 解析JSON响应
            try:
                json_start = response_text.find("{")
                json_end = response_text.rfind("}") + 1
                if json_start >= 0 and json_end > json_start:
                    json_str = response_text[json_start:json_end]
                    result_data = json.loads(json_str)
                else:
                    raise ValueError("No JSON found in response")
            except (json.JSONDecodeError, ValueError) as e:
                logger.error(f"[block_judge] JSON解析失败: {response_text}\n{e}")
                return BlockJudgeResult.no_reply("JSON解析失败")

            result = BlockJudgeResult.from_dict(result_data)

            # 【重要】处理沉默模式转换（在判断结果后立即执行）
            # 这样下一条消息会立即受到沉默模式的影响
            if group_id:
                if result.should_enter_silence_mode:
                    set_silent(group_id, True)
                    logger.warning(
                        f"[block_judge] 🔇 用户表示不想频繁收到回复，进入沉默模式（回复频率会降低）",
                        extra={"group_id": group_id},
                    )
                elif result.should_exit_silence_mode:
                    set_silent(group_id, False)
                    logger.info(
                        f"[block_judge] 🔊 用户希望AI恢复正常回复频率，退出沉默模式",
                        extra={"group_id": group_id},
                    )

            # 记录判断结果
            msg = f"[block_judge] ======== 对话块判断完成 ========"
            logger.info(msg, extra={"group_id": group_id, "should_reply": result.should_reply, "reply_count": result.reply_count})

            msg = f"[block_judge] 块摘要: {result.block_summary}"
            logger.info(msg, extra={"group_id": group_id})

            msg = f"[block_judge] 判断结果: 需要回复={result.should_reply}, 回复次数={result.reply_count}, 原因={result.explanation}"
            logger.info(msg, extra={
                "group_id": group_id,
                "should_reply": result.should_reply,
                "reply_count": result.reply_count,
            })

            # 详细输出每个回复计划
            if result.should_reply and result.replies:
                msg = f"[block_judge] 回复计划详情 (共{len(result.replies)}条回复):"
                logger.info(msg, extra={"group_id": group_id})
                for idx, reply_plan in enumerate(result.replies, 1):
                    msg = f"[block_judge] 【回复 {idx}】态度={reply_plan.emotion}, @用户={reply_plan.target_user_id}, 需要@={reply_plan.should_mention}"
                    logger.info(msg, extra={
                        "group_id": group_id,
                        "reply_index": idx,
                        "emotion": reply_plan.emotion,
                        "target_user_id": reply_plan.target_user_id,
                        "should_mention": reply_plan.should_mention,
                    })
                    msg = f"[block_judge]   指导词: {reply_plan.instruction}"
                    logger.info(msg, extra={"group_id": group_id})
                    msg = f"[block_judge]   针对内容: {reply_plan.related_messages}"
                    logger.info(msg, extra={"group_id": group_id})

            return result

        except Exception as e:
            logger.error(f"[block_judge] 判断出错: {e}", exc_info=True)
            return BlockJudgeResult.no_reply(f"判断出错: {e}")


# 全局单例
block_judger = BlockJudger()

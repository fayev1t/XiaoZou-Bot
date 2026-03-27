"""对话块判断服务 - 分析聚合的消息块，决定回复策略

替代原来的单消息判断（message_judge.py），改为对整个对话块进行判断。
"""

from __future__ import annotations

import importlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from qqbot.core.logging import get_logger, log_ai_input, log_ai_output
from qqbot.services.prompt import PromptManager
from qqbot.services.silence_mode import is_silent, set_silent

if TYPE_CHECKING:
    from qqbot.services.message_aggregator import ResponseBlock

logger = get_logger(__name__)
_TRUE_STRINGS = {"1", "true", "yes", "y", "on"}
_FALSE_STRINGS = {"0", "false", "no", "n", "off", ""}


def _normalize_str(value: object, default: str = "") -> str:
    if value is None:
        return default
    normalized = str(value).strip()
    return normalized or default


def _normalize_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _TRUE_STRINGS:
            return True
        if normalized in _FALSE_STRINGS:
            return False
    return default


def _normalize_optional_int(value: object) -> int | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            return None
        try:
            return int(normalized)
        except ValueError:
            try:
                numeric_value = float(normalized)
            except ValueError:
                return None
            return int(numeric_value) if numeric_value.is_integer() else None
    return None


def _normalize_related_image_hashes(value: object) -> list[str]:
    if isinstance(value, str):
        raw_items: list[object] = [value]
    elif isinstance(value, list):
        raw_items = value
    else:
        return []

    related_image_hashes: list[str] = []
    seen_hashes: set[str] = set()
    for raw_item in raw_items:
        if not isinstance(raw_item, str):
            continue

        normalized = raw_item.strip()
        if not normalized or normalized in seen_hashes:
            continue

        related_image_hashes.append(normalized)
        seen_hashes.add(normalized)

    return related_image_hashes


def _normalize_non_negative_int(value: object, default: int = 0) -> int:
    normalized = _normalize_optional_int(value)
    if normalized is None or normalized < 0:
        return default
    return normalized


@dataclass
class ReplyPlan:
    """单次回复的计划"""

    should_reply: bool = False
    instruction: str = ""
    target_user_id: int | None = None
    should_mention: bool = False
    related_image_hashes: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: object) -> ReplyPlan | None:
        if not isinstance(data, dict):
            return None

        return cls(
            should_reply=_normalize_bool(data.get("should_reply"), default=False),
            instruction=_normalize_str(data.get("instruction")),
            target_user_id=_normalize_optional_int(data.get("target_user_id")),
            should_mention=_normalize_bool(data.get("should_mention"), default=False),
            related_image_hashes=_normalize_related_image_hashes(
                data.get("related_image_hashes")
            ),
        )


@dataclass
class BlockJudgeResult:
    """对话块判断结果"""

    should_reply: bool
    reply_count: int
    topic_count: int
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
        raw_replies = data.get("replies", []) if isinstance(data, dict) else []
        if isinstance(raw_replies, dict):
            raw_replies = [raw_replies]
        if not isinstance(raw_replies, list):
            raw_replies = []

        replies: list[ReplyPlan] = []
        for raw_reply in raw_replies:
            reply_plan = ReplyPlan.from_dict(raw_reply)
            if reply_plan is not None:
                replies.append(reply_plan)

        reply_count = sum(1 for reply in replies if reply.should_reply)
        topic_count = _normalize_non_negative_int(
            data.get("topic_count"),
            default=len(replies),
        )
        if topic_count != len(replies):
            topic_count = len(replies)

        return cls(
            should_reply=reply_count > 0,
            reply_count=reply_count,
            topic_count=topic_count,
            replies=replies,
            explanation=_normalize_str(data.get("explanation", "")),
            should_enter_silence_mode=_normalize_bool(
                data.get("should_enter_silence_mode", False),
                default=False,
            ),
            should_exit_silence_mode=_normalize_bool(
                data.get("should_exit_silence_mode", False),
                default=False,
            ),
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
            topic_count=0,
            replies=[],
            explanation=reason,
        )


def format_block_messages(block: ResponseBlock) -> str:
    return "\n".join(
        msg.formatted_message for msg in block.messages if msg.formatted_message
    )


def build_layer3_context(
    *,
    context: str,
    current_block_text: str,
) -> str:
    sections: list[str] = []

    base_context = context.strip()
    if base_context:
        sections.append(f"【历史上下文】\n{base_context}")

    normalized_block_text = current_block_text.strip()
    if normalized_block_text:
        sections.append(f"【当前对话块】\n{normalized_block_text}")

    if not sections:
        return "（暂无对话上下文）"
    return "\n\n".join(section for section in sections if section.strip())


class BlockJudger:
    """对话块判断服务 - 分析整个对话块决定回复策略"""

    def __init__(self) -> None:
        """初始化判断服务"""
        from qqbot.core.llm import LLMConfig

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

    async def judge_block(
        self,
        block: ResponseBlock,
        context: str,
        group_id: int | None = None,
    ) -> BlockJudgeResult:
        """判断对话块是否需要回复，以及如何回复

        Args:
            block: 对话响应块
            context: 历史上下文（从数据库获取的最近消息）
            group_id: 群ID（用于沉默模式管理）
        Returns:
            BlockJudgeResult包含回复策略
        """
        try:
            # 检查块是否为空
            if block.get_message_count() == 0:
                return BlockJudgeResult.no_reply("对话块为空")

            llm = await self._get_llm()
            if llm is None:
                logger.warning(
                    "[block_judge] LLM unavailable, skip reply judgment",
                    extra={"group_id": group_id},
                )
                return BlockJudgeResult.no_reply("LLM不可用")

            # 检查沉默模式
            silence_mode = is_silent(group_id) if group_id else False

            # 格式化对话块消息
            block_content = format_block_messages(block)

            # 构建用户提示
            user_prompt = f"""【历史上下文】
{context}

【当前对话块】（{block.get_message_count()}条消息，来自{len(block.get_unique_users())}个用户）
{block_content}

请分析这个对话块并判断：
1. 是否需要回复？
2. 当前块里有几个彼此独立、可区分的话题？
3. 每个主题是否应该实际发送回复、主要在回谁、要不要@、要给 Layer 3 看哪些当前块图片 file_hash？
4. 必须先分清谁在对谁说话、谁是主要回复对象、谁只是补充或背景。
5. 用户是否在抱怨AI说话太多？
6. 用户是否在催促AI说话？

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
            messages_module = importlib.import_module("langchain_core.messages")
            human_message_class = messages_module.HumanMessage
            system_message_class = messages_module.SystemMessage

            messages = [
                system_message_class(content=system_prompt),
                human_message_class(content=user_prompt),
            ]

            # 记录 AI 输入（只记录消息块内容，不记录历史上下文和提示词）
            log_input = f"【消息块】{block.get_message_count()}条消息，{len(block.get_unique_users())}个用户\n{block_content}"
            if silence_mode:
                log_input += "\n【沉默模式】已激活"
            log_ai_input("Layer2", group_id or 0, log_input)

            response = await llm.ainvoke(messages)
            response_text = response.content.strip()

            # 记录 AI 输出
            log_ai_output("Layer2", group_id or 0, response_text)

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
            logger.info(
                msg,
                extra={
                    "group_id": group_id,
                    "should_reply": result.should_reply,
                    "reply_count": result.reply_count,
                    "topic_count": result.topic_count,
                },
            )

            msg = (
                "[block_judge] 判断结果: "
                f"需要回复={result.should_reply}, "
                f"主题数={result.topic_count}, "
                f"实际回复数={result.reply_count}, "
                f"原因={result.explanation}"
            )
            logger.info(
                msg,
                extra={
                    "group_id": group_id,
                    "should_reply": result.should_reply,
                    "reply_count": result.reply_count,
                    "topic_count": result.topic_count,
                },
            )

            # 详细输出每个回复计划
            if result.replies:
                msg = f"[block_judge] 回复计划详情 (共{len(result.replies)}个主题):"
                logger.info(msg, extra={"group_id": group_id})
                for idx, reply_plan in enumerate(result.replies, 1):
                    msg = (
                        f"[block_judge] 【主题 {idx}】"
                        f"should_reply={reply_plan.should_reply}, "
                        f"@用户={reply_plan.target_user_id}, "
                        f"需要@={reply_plan.should_mention}, "
                        f"关联图片数={len(reply_plan.related_image_hashes)}"
                    )
                    logger.info(
                        msg,
                        extra={
                            "group_id": group_id,
                            "reply_index": idx,
                            "topic_should_reply": reply_plan.should_reply,
                            "target_user_id": reply_plan.target_user_id,
                            "should_mention": reply_plan.should_mention,
                            "related_image_count": len(
                                reply_plan.related_image_hashes
                            ),
                        },
                    )
                    msg = f"[block_judge]   指导词: {reply_plan.instruction}"
                    logger.info(msg, extra={"group_id": group_id})
                    if reply_plan.related_image_hashes:
                        msg = (
                            "[block_judge]   关联图片哈希: "
                            f"{', '.join(reply_plan.related_image_hashes)}"
                        )
                        logger.info(msg, extra={"group_id": group_id})

            return result

        except Exception as e:
            logger.error(f"[block_judge] 判断出错: {e}", exc_info=True)
            return BlockJudgeResult.no_reply(f"判断出错: {e}")


# 全局单例
block_judger = BlockJudger()

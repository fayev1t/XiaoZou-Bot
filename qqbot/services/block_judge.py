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


def _normalize_non_negative_int(value: object, default: int = 0) -> int:
    normalized = _normalize_optional_int(value)
    if normalized is None or normalized < 0:
        return default
    return normalized


@dataclass
class ToolCallRequest:
    tool: str
    input: str
    msg_hash: str

    @classmethod
    def from_dict(cls, data: object) -> ToolCallRequest | None:
        if not isinstance(data, dict):
            return None

        tool = _normalize_str(data.get("tool"))
        input_data = _normalize_str(data.get("input"))
        msg_hash = _normalize_str(data.get("msg_hash"))
        if not tool or not input_data or not msg_hash:
            return None
        return cls(tool=tool, input=input_data, msg_hash=msg_hash)


def _normalize_tool_calls(value: object) -> list[ToolCallRequest]:
    if not isinstance(value, list):
        return []

    tool_calls: list[ToolCallRequest] = []
    seen_items: set[tuple[str, str, str]] = set()
    for raw_item in value:
        tool_call = ToolCallRequest.from_dict(raw_item)
        if tool_call is None:
            continue
        dedupe_key = (tool_call.tool, tool_call.input, tool_call.msg_hash)
        if dedupe_key in seen_items:
            continue
        seen_items.add(dedupe_key)
        tool_calls.append(tool_call)
    return tool_calls


@dataclass
class ReplyPlan:
    should_reply: bool = False
    instruction: str = ""
    target_user_id: int | None = None
    should_mention: bool = False
    tool_calls: list[ToolCallRequest] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: object) -> ReplyPlan | None:
        if not isinstance(data, dict):
            return None

        return cls(
            should_reply=_normalize_bool(data.get("should_reply"), default=False),
            instruction=_normalize_str(data.get("instruction")),
            target_user_id=_normalize_optional_int(data.get("target_user_id")),
            should_mention=_normalize_bool(data.get("should_mention"), default=False),
            tool_calls=_normalize_tool_calls(data.get("tool_calls")),
        )


@dataclass
class BlockJudgeResult:
    should_reply: bool
    reply_count: int
    topic_count: int
    replies: list[ReplyPlan] = field(default_factory=list)
    explanation: str = ""
    should_enter_silence_mode: bool = False
    should_exit_silence_mode: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BlockJudgeResult:
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
    def no_reply(cls, reason: str = "") -> BlockJudgeResult:
        return cls(
            should_reply=False,
            reply_count=0,
            topic_count=0,
            replies=[],
            explanation=reason,
        )


def _build_parse_failed_placeholder(reason: str = "消息未完成格式化") -> str:
    normalized_reason = reason.strip() if reason else "消息不可用"
    return f"【解析失败：{normalized_reason}】"


def format_block_messages(block: ResponseBlock) -> str:
    return "\n".join(
        (
            msg.formatted_message
            or _build_parse_failed_placeholder("消息未完成格式化")
        )
        for msg in block.messages
        if (
            msg.formatted_message
            or _build_parse_failed_placeholder("消息未完成格式化")
        )
    )


def build_layer3_context(
    *,
    context: str,
    current_block_text: str,
    tool_call_xmls: list[str] | None = None,
) -> str:
    sections: list[str] = []

    base_context = context.strip()
    if base_context:
        sections.append(f"【历史上下文】\n{base_context}")

    normalized_block_text = current_block_text.strip()
    if normalized_block_text:
        sections.append(f"【当前对话块】\n{normalized_block_text}")

    if tool_call_xmls:
        normalized_xmls = [xml.strip() for xml in tool_call_xmls if xml.strip()]
        if normalized_xmls:
            sections.append("【当前工具调用结果】\n" + "\n".join(normalized_xmls))

    if not sections:
        return "（暂无对话上下文）"
    return "\n\n".join(section for section in sections if section.strip())


class BlockJudger:
    def __init__(self) -> None:
        from qqbot.core.llm import LLMConfig

        self.config = LLMConfig()
        self.prompt_manager = PromptManager()
        self._llm = None

    async def _get_llm(self) -> Any:
        if self._llm is None:
            from qqbot.core.llm import create_llm

            self._llm = await create_llm(temperature=0.5)
        return self._llm

    async def judge_block(
        self,
        block: ResponseBlock,
        context: str,
        group_id: int | None = None,
        current_block_text: str | None = None,
    ) -> BlockJudgeResult:
        try:
            if block.get_message_count() == 0:
                return BlockJudgeResult.no_reply("对话块为空")

            llm = await self._get_llm()
            if llm is None:
                logger.warning(
                    "[block_judge] LLM unavailable, skip reply judgment",
                    extra={"group_id": group_id},
                )
                return BlockJudgeResult.no_reply("LLM不可用")

            silence_mode = is_silent(group_id) if group_id else False
            block_content = current_block_text or format_block_messages(block)

            user_prompt = f"""【历史上下文】
{context}

【当前对话块】（{block.get_message_count()}条消息，来自{len(block.get_unique_users())}个用户）
{block_content}

请分析这个对话块并判断：
1. 是否需要回复？
2. 当前块里有几个彼此独立、可区分的话题？
3. 每个主题是否应该实际发送回复、主要在回谁、要不要@、以及要执行哪些工具调用？
4. 必须先分清谁在对谁说话、谁是主要回复对象、谁只是补充或背景。
5. 用户是否在抱怨AI说话太多？
6. 用户是否在催促AI说话？

输出JSON格式的判断结果。"""

            system_prompt = self.prompt_manager.block_judge_prompt
            if silence_mode:
                system_prompt += "\n\n【特殊状态：沉默模式激活】当前群处于沉默模式，回复的判定标准应该变得严格。在这个模式下，只有在以下情况才应该判定为需要回复：用户明确@你、用户提出知识型问题、或有重要信息需要传达。其他的闲聊、话题讨论等应该判定为'不回复'。"

            logger.info(
                f"[block_judge] Judging block with {block.get_message_count()} messages",
                extra={
                    "group_id": group_id,
                    "message_count": block.get_message_count(),
                    "unique_users": len(block.get_unique_users()),
                    "silence_mode": silence_mode,
                },
            )

            messages_module = importlib.import_module("langchain_core.messages")
            human_message_class = messages_module.HumanMessage
            system_message_class = messages_module.SystemMessage

            messages = [
                system_message_class(content=system_prompt),
                human_message_class(content=user_prompt),
            ]

            log_input = (
                f"【消息块】{block.get_message_count()}条消息，{len(block.get_unique_users())}个用户\n{block_content}"
            )
            if silence_mode:
                log_input += "\n【沉默模式】已激活"
            log_ai_input("Layer2", group_id or 0, log_input)

            response = await llm.ainvoke(messages)
            response_text = response.content.strip()
            log_ai_output("Layer2", group_id or 0, response_text)

            try:
                json_start = response_text.find("{")
                json_end = response_text.rfind("}") + 1
                if json_start >= 0 and json_end > json_start:
                    json_str = response_text[json_start:json_end]
                    result_data = json.loads(json_str)
                else:
                    raise ValueError("No JSON found in response")
            except (json.JSONDecodeError, ValueError) as exc:
                logger.error(f"[block_judge] JSON解析失败: {response_text}\n{exc}")
                return BlockJudgeResult.no_reply("JSON解析失败")

            result = BlockJudgeResult.from_dict(result_data)

            if group_id:
                if result.should_enter_silence_mode:
                    set_silent(group_id, True)
                    logger.warning(
                        "[block_judge] 🔇 用户表示不想频繁收到回复，进入沉默模式（回复频率会降低）",
                        extra={"group_id": group_id},
                    )
                elif result.should_exit_silence_mode:
                    set_silent(group_id, False)
                    logger.info(
                        "[block_judge] 🔊 用户希望AI恢复正常回复频率，退出沉默模式",
                        extra={"group_id": group_id},
                    )

            logger.info(
                "[block_judge] ======== 对话块判断完成 ========",
                extra={
                    "group_id": group_id,
                    "should_reply": result.should_reply,
                    "reply_count": result.reply_count,
                    "topic_count": result.topic_count,
                },
            )
            logger.info(
                "[block_judge] 判断结果: 需要回复=%s, 主题数=%s, 实际回复数=%s, 原因=%s",
                result.should_reply,
                result.topic_count,
                result.reply_count,
                result.explanation,
                extra={
                    "group_id": group_id,
                    "should_reply": result.should_reply,
                    "reply_count": result.reply_count,
                    "topic_count": result.topic_count,
                },
            )

            if result.replies:
                logger.info(
                    f"[block_judge] 回复计划详情 (共{len(result.replies)}个主题):",
                    extra={"group_id": group_id},
                )
                for idx, reply_plan in enumerate(result.replies, 1):
                    logger.info(
                        "[block_judge] 【主题 %s】should_reply=%s, @用户=%s, 需要@=%s, 工具调用数=%s",
                        idx,
                        reply_plan.should_reply,
                        reply_plan.target_user_id,
                        reply_plan.should_mention,
                        len(reply_plan.tool_calls),
                        extra={
                            "group_id": group_id,
                            "reply_index": idx,
                            "topic_should_reply": reply_plan.should_reply,
                            "target_user_id": reply_plan.target_user_id,
                            "should_mention": reply_plan.should_mention,
                            "tool_call_count": len(reply_plan.tool_calls),
                        },
                    )
                    logger.info(
                        f"[block_judge]   指导词: {reply_plan.instruction}",
                        extra={"group_id": group_id},
                    )
                    if reply_plan.tool_calls:
                        logger.info(
                            "[block_judge]   工具调用: %s",
                            ", ".join(
                                f"{tool_call.tool}:{tool_call.input}@{tool_call.msg_hash}"
                                for tool_call in reply_plan.tool_calls
                            ),
                            extra={"group_id": group_id},
                        )

            return result

        except Exception as exc:
            logger.error(f"[block_judge] 判断出错: {exc}", exc_info=True)
            return BlockJudgeResult.no_reply(f"判断出错: {exc}")


block_judger = BlockJudger()

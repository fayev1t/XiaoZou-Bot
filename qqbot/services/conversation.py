"""Conversation service for generating AI responses (Layer 3 AI)."""

import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from sqlalchemy.ext.asyncio import AsyncSession

from qqbot.core.llm import LLMConfig, create_llm
from qqbot.core.logging import get_logger, log_ai_input, log_ai_output
from qqbot.services.group_member import GroupMemberService
from qqbot.services.prompt import PromptManager
from qqbot.services.user import UserService

logger = get_logger(__name__)
_SYSTEM_XML_TAG_RE = re.compile(r"</?System-[^>]*>")


class ConversationService:
    """Service for generating Layer 3 responses from context and instruction."""

    def __init__(self) -> None:
        """Initialize the conversation service."""
        self.config = LLMConfig()
        self.prompt_manager = PromptManager()
        self._llm = None

    async def _get_llm(self) -> Any:
        """Get or create LLM instance.

        Returns:
            LLM instance for making API calls
        """
        if self._llm is None:
            self._llm = await create_llm(temperature=0.9)
        return self._llm

    async def generate_response(
        self,
        session: AsyncSession | None,
        context: str,
        instruction: str,
        target_user_id: int | None = None,
        should_mention: bool = False,
        group_id: int | None = None,
        image_inputs: list[dict[str, Any]] | None = None,
    ) -> str:
        """Generate a response from Layer 2 instruction and context.

        Args:
            session: Database session
            context: Formatted message context
            instruction: Direct instruction from Layer 2
            target_user_id: Target user id for optional @ mention
            should_mention: Whether to prepend a CQ @ mention
            group_id: Group ID for looking up member info (optional)

        Returns:
            Generated response text (with @ mention if applicable)
        """
        try:
            llm = await self._get_llm()
            if llm is None:
                logger.warning(
                    "[conversation] LLM unavailable, returning fallback response",
                    extra={"group_id": group_id},
                )
                return "喵~让我想想..."

            normalized_context = context.strip() or "（暂无对话上下文）"
            normalized_instruction = instruction.strip() or "请自然承接当前话题。"
            tool_call_count = normalized_context.count("<System-ToolCall")
            normalized_image_inputs = image_inputs or []

            response_prompt = f"""【对话背景】
{normalized_context}

【当前指导】
{normalized_instruction}"""

            response_prompt += "\n\n请根据以上信息生成一条自然的群聊回复。注意保持小奏的人格特征。"

            logger.info(
                "[Layer3] 生成回复",
                extra={
                    "group_id": group_id,
                    "target_user_id": target_user_id,
                    "should_mention": should_mention,
                    "tool_call_count": tool_call_count,
                    "image_input_count": len(normalized_image_inputs),
                },
            )

            log_input = (
                f"【指导】{normalized_instruction}\n"
                f"【上下文统计】长度={len(normalized_context)}，工具结果数={tool_call_count}，图片数={len(normalized_image_inputs)}"
            )
            log_ai_input("Layer3", group_id or 0, log_input)

            generated_text = await self._invoke_response_llm(
                llm,
                response_prompt,
                image_inputs=normalized_image_inputs,
            )

            # 记录 AI 输出
            log_ai_output("Layer3", group_id or 0, generated_text)

            generated_text = self._strip_system_xml_tags(generated_text)

            # Add @mention prefix if replying to a specific person AND should_mention=true
            if target_user_id and should_mention and group_id:
                try:
                    target_session = session
                    if target_session is None:
                        from qqbot.core.database import AsyncSessionLocal

                        async with AsyncSessionLocal() as mention_session:
                            target_name = await self._resolve_target_name(
                                session=mention_session,
                                group_id=group_id,
                                target_user_id=target_user_id,
                            )
                    else:
                        target_name = await self._resolve_target_name(
                            session=target_session,
                            group_id=group_id,
                            target_user_id=target_user_id,
                        )

                    prefix = f"[CQ:at,qq={target_user_id}] "
                    generated_text = prefix + generated_text

                    logger.debug(
                        "[ConversationService] 【第三层AI】添加@提及",
                        extra={
                            "layer": "response",
                            "target_user_id": target_user_id,
                            "target_name": target_name,
                        },
                    )

                except Exception as e:
                    logger.debug(f"[conversation] Failed to add mention: {e}")
                    # Continue without mention if lookup fails

            logger.debug(f"[conversation] Response generated ({len(generated_text)} chars): {generated_text[:50]}...")

            return generated_text

        except Exception as e:
            logger.error(f"[ConversationService] 【第三层AI】调用出错: {e}", exc_info=True)
            # Return fallback response on error
            return "喵~让我想想..."

    async def _resolve_target_name(
        self,
        session: AsyncSession,
        group_id: int,
        target_user_id: int,
    ) -> str:
        user_service = UserService(session)
        member_service = GroupMemberService(session)
        target_user = await user_service.get_user(target_user_id)
        target_member = await member_service.get_member(group_id, target_user_id)
        target_card = target_member.get("card") if target_member else None
        return target_card or (target_user.nickname if target_user else None) or f"用户{target_user_id}"

    def _strip_system_xml_tags(self, text: str) -> str:
        cleaned = _SYSTEM_XML_TAG_RE.sub("", text)
        cleaned = cleaned.strip()
        return cleaned or "..."

    async def _invoke_response_llm(
        self,
        llm: Any,
        response_prompt: str,
        image_inputs: list[dict[str, Any]] | None = None,
    ) -> str:
        messages = self._build_messages(
            response_prompt=response_prompt,
            image_inputs=image_inputs,
        )
        try:
            response = await llm.ainvoke(messages)
        except Exception:
            if image_inputs:
                logger.warning(
                    "[conversation] multimodal response failed, fallback to text-only"
                )
                response = await llm.ainvoke(
                    self._build_messages(response_prompt=response_prompt)
                )
            else:
                raise
        return self._extract_text_response(response.content)

    def _build_messages(
        self,
        response_prompt: str,
        image_inputs: list[dict[str, Any]] | None = None,
    ) -> list[SystemMessage | HumanMessage]:
        human_content: str | list[dict[str, Any]] = response_prompt
        if image_inputs:
            human_content = [{"type": "text", "text": response_prompt}, *image_inputs]
        return [
            SystemMessage(content=self.prompt_manager.response_prompt),
            HumanMessage(content=human_content),
        ]

    def _extract_text_response(self, content: object) -> str:
        if isinstance(content, str):
            normalized = content.strip()
            if normalized:
                return normalized
            raise ValueError("empty response content")

        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, str) and item.strip():
                    parts.append(item.strip())
                    continue

                if isinstance(item, dict):
                    text = item.get("text")
                    if isinstance(text, str) and text.strip():
                        parts.append(text.strip())

            merged = "\n".join(parts).strip()
            if merged:
                return merged
            raise ValueError("empty structured response content")

        raise TypeError(f"unsupported response content type: {type(content).__name__}")


# 全局单例
conversation_service = ConversationService()

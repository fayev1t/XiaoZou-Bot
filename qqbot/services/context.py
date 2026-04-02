from __future__ import annotations

from datetime import datetime
import re

from sqlalchemy.ext.asyncio import AsyncSession

from qqbot.core.settings import get_primary_bot_name
from qqbot.services.group_member import GroupMemberService
from qqbot.services.group_message import GroupMessageService
from qqbot.services.message_converter import MessageConverter
from qqbot.services.message_format_fallback import build_parse_failure_text
from qqbot.services.tool_call_record import ToolCallRecordService, build_system_tool_call_xml
from qqbot.services.user import UserService

BOT_DISPLAY_NAME = get_primary_bot_name()

class ContextManager:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def parse_at_info(
        self,
        group_id: int,
        message_content: str,
    ) -> str:
        at_pattern = r"\[CQ:at,qq=(\d+)\]"
        matches = re.finditer(at_pattern, message_content)

        result = message_content
        for match in matches:
            at_qq = int(match.group(1))
            cq_code = match.group(0)

            try:
                user_service = UserService(self.session)
                user = await user_service.get_user(at_qq)
                user_nickname = user.nickname if user and user.nickname else f"用户{at_qq}"
            except Exception:
                user_nickname = f"用户{at_qq}"

            try:
                member_service = GroupMemberService(self.session)
                member = await member_service.get_member(group_id, at_qq)
                group_card = member.get("card") if member else None
            except Exception:
                group_card = None

            display_name = group_card or user_nickname or f"用户{at_qq}"
            replacement = f"@{at_qq}(显示名:{display_name})"

            result = result.replace(cq_code, replacement)

        return result

    async def get_recent_context(
        self,
        group_id: int,
        limit: int = 30,
        bot_id: int | None = None,
        before_message_id: int | None = None,
    ) -> str:
        message_service = GroupMessageService(self.session)
        messages = await message_service.get_recent_messages(
            group_id=group_id,
            limit=limit,
            before_message_id=before_message_id,
        )

        if not messages:
            return "（暂无上下文消息）"

        return await self.render_context_messages(
            group_id=group_id,
            messages=messages,
            bot_id=bot_id,
        )

    async def render_context_messages(
        self,
        *,
        group_id: int,
        messages: list[dict[str, object]],
        bot_id: int | None = None,
    ) -> str:
        if not messages:
            return "（暂无上下文消息）"

        converter = MessageConverter()
        tool_call_service = ToolCallRecordService(self.session)
        msg_hashes = [
            str(message.get("msg_hash") or "").strip()
            for message in messages
            if str(message.get("msg_hash") or "").strip()
        ]
        records_by_msg_hash = await tool_call_service.get_records_by_msg_hashes(msg_hashes)

        context_lines: list[str] = []
        for message in messages:
            if message.get("is_recalled", False):
                continue

            msg_hash = str(message.get("msg_hash") or "").strip()
            rendered_message = await self._render_message_line(
                group_id=group_id,
                message=message,
                bot_id=bot_id,
                converter=converter,
                msg_hash=msg_hash,
            )
            context_lines.append(rendered_message)

            for record in records_by_msg_hash.get(msg_hash, []):
                context_lines.append(
                    build_system_tool_call_xml(
                        call_hash=record.call_hash,
                        tool_name=record.tool_name,
                        input_data=record.input_data,
                        output_data=record.output_data,
                    )
                )

        return "\n".join(context_lines) if context_lines else "（暂无上下文消息）"

    async def _render_message_line(
        self,
        *,
        group_id: int,
        message: dict[str, object],
        bot_id: int | None,
        converter: MessageConverter,
        msg_hash: str,
    ) -> str:
        formatted_message = message.get("formatted_message")
        if isinstance(formatted_message, str) and formatted_message.strip():
            return formatted_message

        user_id = message.get("user_id")
        raw_timestamp = message.get("timestamp")
        timestamp = (
            raw_timestamp
            if isinstance(raw_timestamp, (datetime, int, float))
            else None
        )
        failure_text = build_parse_failure_text("历史消息未完成格式化")

        if bot_id and user_id == bot_id:
            return converter.wrap_plain_text(
                failure_text,
                msg_hash=msg_hash,
                user_id=user_id if isinstance(user_id, int) else None,
                display_name=BOT_DISPLAY_NAME,
                timestamp=timestamp,
            )
        return converter.wrap_plain_text(
            failure_text,
            msg_hash=msg_hash,
            user_id=user_id if isinstance(user_id, int) else None,
            timestamp=timestamp,
        )

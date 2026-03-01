"""Context extraction and formatting for conversation system."""

from dataclasses import dataclass
from datetime import datetime
import re

from nonebot.adapters.onebot.v11 import Message
from sqlalchemy.ext.asyncio import AsyncSession

from qqbot.services.group_member import GroupMemberService
from qqbot.services.group_message import GroupMessageService
from qqbot.services.message_converter import MessageConverter
from qqbot.services.user import UserService


@dataclass
class LegacyEvent:
    """Minimal event shim for formatting legacy messages."""

    group_id: int
    user_id: int | None
    message: Message
    raw_message: str
    time: datetime | str | None = None


class ContextManager:
    """Extract and format group chat context for AI analysis."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def parse_at_info(
        self,
        group_id: int,
        message_content: str,
    ) -> str:
        """Parse CQ code @mentions and convert them to readable format.

        Converts [CQ:at,qq=123456] to @123456(显示名:昵称)

        Args:
            group_id: QQ group ID
            message_content: Message content with CQ codes

        Returns:
            Message with @mentions converted to readable format
        """
        # Find all CQ at codes
        at_pattern = r"\[CQ:at,qq=(\d+)\]"
        matches = re.finditer(at_pattern, message_content)

        result = message_content
        for match in matches:
            at_qq = int(match.group(1))
            cq_code = match.group(0)

            # Get the user's display name
            try:
                user_service = UserService(self.session)
                user = await user_service.get_user(at_qq)
                user_nickname = user.get("nickname") if user else f"用户{at_qq}"
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
    ) -> str:
        """Get recent message context before the current point.

        Fetches the most recent messages and returns System-Message XML lines.

        Args:
            group_id: QQ group ID
            limit: Number of recent messages to fetch (default: 30)
            bot_id: Bot's QQ ID for identifying bot messages (optional)

        Returns:
            Formatted context string with recent messages
        """
        # Get recent messages from database
        message_service = GroupMessageService(self.session)
        messages = await message_service.get_recent_messages(
            group_id=group_id,
            limit=limit,
        )

        if not messages:
            return "（暂无上下文消息）"

        converter = MessageConverter()
        context_lines: list[str] = []

        for msg in messages:
            # Skip recalled messages
            is_recalled = msg.get("is_recalled", False)
            if is_recalled:
                continue

            formatted_message = msg.get("formatted_message")
            if formatted_message:
                context_lines.append(formatted_message)
                continue

            raw_message = (
                msg.get("raw_message") or msg.get("message_content", "") or ""
            )
            user_id = msg.get("user_id")
            timestamp = msg.get("timestamp")

            if not raw_message:
                raw_message = "【空消息】"

            if bot_id and user_id == bot_id:
                fallback = converter.wrap_plain_text(
                    raw_message,
                    user_id=user_id,
                    display_name="小奏",
                    timestamp=timestamp,
                )
                context_lines.append(fallback)
                continue

            try:
                legacy_event = LegacyEvent(
                    group_id=group_id,
                    user_id=user_id,
                    message=Message(raw_message),
                    raw_message=raw_message,
                    time=timestamp,
                )
                converted = await converter.convert_event(self.session, legacy_event)
                context_lines.append(converted.content)
            except Exception:
                if "[CQ:at,qq=" in raw_message:
                    try:
                        raw_message = await self.parse_at_info(
                            group_id, raw_message
                        )
                    except Exception:
                        pass
                fallback = converter.wrap_plain_text(
                    raw_message,
                    user_id=user_id,
                    timestamp=timestamp,
                )
                context_lines.append(fallback)

        return "\n".join(context_lines)

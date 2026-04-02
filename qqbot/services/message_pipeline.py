"""Message processing pipeline for conversion and persistence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from html import escape

from nonebot.adapters.onebot.v11 import GroupMessageEvent
from sqlalchemy.ext.asyncio import AsyncSession

from qqbot.core.ids import new_msg_hash
from qqbot.core.time import normalize_china_time
from qqbot.services.group import GroupService
from qqbot.services.group_message import GroupMessageService
from qqbot.services.message_converter import MessageConverter
from qqbot.services.user import UserService


@dataclass
class MessageRecord:
    """Message data after conversion."""

    group_id: int
    user_id: int
    msg_hash: str
    onebot_message_id: str | None
    raw_message: str
    formatted_message: str | None
    timestamp: datetime
    message_type: str | None


class MessagePipeline:
    """Pipeline for building and saving formatted messages."""

    def __init__(self, converter: MessageConverter | None = None) -> None:
        self.converter = converter or MessageConverter()

    def extract_raw(self, event: GroupMessageEvent) -> str:
        original_message = getattr(event, "original_message", None)
        if original_message is not None:
            raw_message = str(original_message)
            if raw_message:
                return raw_message

        raw_message = getattr(event, "raw_message", "") or ""
        if raw_message:
            return raw_message

        return str(event.message)

    def _extract_onebot_message_id(self, event: GroupMessageEvent) -> str | None:
        message_id = getattr(event, "message_id", None)
        if message_id is None:
            return None

        normalized = str(message_id).strip()
        return normalized or None

    def _build_base_record(
        self,
        event: GroupMessageEvent,
        raw_message: str,
        msg_hash: str,
    ) -> MessageRecord:
        return MessageRecord(
            group_id=event.group_id,
            user_id=event.user_id,
            msg_hash=msg_hash,
            onebot_message_id=self._extract_onebot_message_id(event),
            raw_message=raw_message,
            formatted_message=None,
            timestamp=normalize_china_time(getattr(event, "time", None)),
            message_type=None,
        )

    def create_raw_record(
        self,
        event: GroupMessageEvent,
        raw_message: str,
    ) -> MessageRecord:
        return self._build_base_record(event, raw_message, new_msg_hash())

    async def persist_raw(
        self,
        session: AsyncSession,
        record: MessageRecord,
    ) -> int:
        user_service = UserService(session)
        group_service = GroupService(session)
        message_service = GroupMessageService(session)

        await user_service.get_or_create_user(record.user_id)
        await group_service.get_or_create_group(record.group_id)

        return await message_service.save_message(
            group_id=record.group_id,
            user_id=record.user_id,
            msg_hash=record.msg_hash,
            onebot_message_id=record.onebot_message_id,
            raw_message=record.raw_message,
            formatted_message=None,
            timestamp=record.timestamp,
        )

    async def format_and_update(
        self,
        session: AsyncSession,
        event: GroupMessageEvent,
        saved_id: int,
        msg_hash: str,
        raw_message: str | None = None,
    ) -> MessageRecord:
        normalized_raw_message = raw_message or self.extract_raw(event)
        try:
            conversion = await self.converter.convert_event(
                session,
                event,
                msg_hash=msg_hash,
            )
            formatted_content = conversion.content
            message_type = conversion.message_type
        except Exception:
            formatted_content = self._build_parse_failed_message(event, msg_hash=msg_hash)
            message_type = "unknown"

        record = self._build_base_record(event, normalized_raw_message, msg_hash)
        record.formatted_message = formatted_content
        record.message_type = message_type

        message_service = GroupMessageService(session)
        await message_service.update_formatted_message(
            group_id=record.group_id,
            local_message_id=saved_id,
            formatted_message=formatted_content,
        )

        return record

    def _build_parse_failed_message(
        self,
        event: GroupMessageEvent,
        *,
        msg_hash: str,
    ) -> str:
        user_id = getattr(event, "user_id", None)
        display_name = f"用户{user_id}" if user_id is not None else "无"
        raw_time = getattr(event, "time", None)
        timestamp = (
            normalize_china_time(raw_time).strftime("%Y-%m-%d %H:%M:%S")
            if raw_time is not None
            else ""
        )
        content = escape("【解析失败：消息格式化异常】", quote=True)
        return (
            f'<System-Message msg_hash="{escape(msg_hash, quote=True)}" '
            f'user_id="{escape(str(user_id) if user_id is not None else "", quote=True)}" '
            f'display_name="{escape(display_name, quote=True)}" '
            f'timestamp="{escape(timestamp, quote=True)}">'
            f"<System-PureText>{content}</System-PureText>"
            "</System-Message>"
        )

    async def process_event(
        self,
        session: AsyncSession,
        event: GroupMessageEvent,
    ) -> tuple[MessageRecord, int]:
        raw_message = self.extract_raw(event)
        record = self.create_raw_record(event, raw_message)
        saved_id = await self.persist_raw(session, record)
        record = await self.format_and_update(
            session,
            event,
            saved_id,
            msg_hash=record.msg_hash,
            raw_message=raw_message,
        )
        return record, saved_id

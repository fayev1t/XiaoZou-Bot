"""Message processing pipeline for conversion and persistence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from nonebot.adapters.onebot.v11 import GroupMessageEvent
from sqlalchemy.ext.asyncio import AsyncSession

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
    onebot_message_id: str | None
    raw_message: str
    formatted_message: str
    timestamp: datetime
    message_type: str


class MessagePipeline:
    """Pipeline for building and saving formatted messages."""

    def __init__(self, converter: MessageConverter | None = None) -> None:
        self.converter = converter or MessageConverter()

    def _extract_raw_message(self, event: GroupMessageEvent) -> str:
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

    async def build_record(
        self,
        session: AsyncSession,
        event: GroupMessageEvent,
    ) -> MessageRecord:
        raw_message = self._extract_raw_message(event)
        conversion = await self.converter.convert_event(session, event)

        return MessageRecord(
            group_id=event.group_id,
            user_id=event.user_id,
            onebot_message_id=self._extract_onebot_message_id(event),
            raw_message=raw_message,
            formatted_message=conversion.content,
            timestamp=normalize_china_time(getattr(event, "time", None)),
            message_type=conversion.message_type,
        )

    async def persist_record(
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
            onebot_message_id=record.onebot_message_id,
            raw_message=record.raw_message,
            formatted_message=record.formatted_message,
            timestamp=record.timestamp,
        )

    async def process_event(
        self,
        session: AsyncSession,
        event: GroupMessageEvent,
    ) -> tuple[MessageRecord, int]:
        record = await self.build_record(session, event)
        saved_id = await self.persist_record(session, record)
        return record, saved_id

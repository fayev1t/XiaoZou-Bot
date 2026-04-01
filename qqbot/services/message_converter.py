"""Message converter for OneBot segments to System-XML format."""

from __future__ import annotations

from datetime import datetime
from dataclasses import dataclass
from html import escape

from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageSegment
from sqlalchemy.ext.asyncio import AsyncSession

from qqbot.core.logging import get_logger
from qqbot.core.time import china_now, normalize_china_time
from qqbot.services.display_name_resolver import DisplayNameResolver
from qqbot.services.group_message import GroupMessageService
from qqbot.services.image_parsing import ImageParsingService

logger = get_logger(__name__)


@dataclass
class MessageConversionResult:
    """Result of message conversion."""

    content: str
    message_type: str


class MessageConverter:
    """Convert OneBot message segments to System-XML format."""

    async def convert_event(
        self,
        session: AsyncSession,
        event: GroupMessageEvent,
    ) -> MessageConversionResult:
        parts: list[str] = []
        message_type = "text"
        name_cache: dict[int, str] = {}
        original_message = getattr(event, "original_message", None)
        message_source = original_message or event.message
        sender_display_name = await self._resolve_sender_name(
            session=session,
            event=event,
            name_cache=name_cache,
        )
        message_time = self._format_message_time(getattr(event, "time", None))

        for segment in message_source:
            seg_type = segment.type
            try:
                part, message_type = await self._convert_segment(
                    session=session,
                    event=event,
                    segment=segment,
                    message_type=message_type,
                    name_cache=name_cache,
                )
            except Exception as exc:
                logger.warning(
                    "[message_converter] failed to convert segment",
                    extra={
                        "group_id": getattr(event, "group_id", None),
                        "segment_type": seg_type,
                        "error": str(exc),
                    },
                )
                part = self._wrap_unknown(
                    "segment",
                    getattr(event, "raw_message", "") or "",
                )

            if part:
                parts.append(part)

        if not parts:
            raw_message = ""
            if original_message is not None:
                raw_message = str(original_message)
            if not raw_message:
                raw_message = getattr(event, "raw_message", "") or ""
            if not raw_message:
                raw_message = str(message_source)
            if raw_message:
                parts.append(self._wrap_unknown("raw", raw_message))
            else:
                parts.append(self._wrap("System-PureText", "【空消息】"))

        user_id = getattr(event, "user_id", None)
        content = self._wrap_message(
            content="".join(parts),
            user_id=user_id,
            display_name=sender_display_name,
            timestamp=message_time,
        )
        return MessageConversionResult(content=content, message_type=message_type)

    def wrap_plain_text(
        self,
        text: str,
        user_id: int | None = None,
        display_name: str | None = None,
        timestamp: datetime | int | float | None = None,
    ) -> str:
        """Wrap plain text content into System-Message format."""
        if display_name is None:
            if user_id is None:
                display_name = "无"
            else:
                display_name = f"用户{user_id}"

        if timestamp is None:
            timestamp = china_now()

        content = self._wrap("System-PureText", text)
        return self._wrap_message(
            content=content,
            user_id=user_id,
            display_name=display_name,
            timestamp=self._format_message_time(timestamp),
        )

    async def _convert_segment(
        self,
        session: AsyncSession,
        event: GroupMessageEvent,
        segment: MessageSegment,
        message_type: str,
        name_cache: dict[int, str],
    ) -> tuple[str, str]:
        seg_type = segment.type
        seg_data = segment.data

        if seg_type == "text":
            text = seg_data.get("text", "")
            if not text:
                return "", message_type
            return self._wrap("System-PureText", text), message_type

        if seg_type == "at":
            user_id = seg_data.get("qq")
            display_name = await DisplayNameResolver.resolve(
                session=session,
                group_id=event.group_id,
                user_id=user_id,
                name_cache=name_cache,
            )
            return (
                self._wrap("System-At", display_name, {"user_id": user_id}),
                message_type,
            )

        if seg_type == "reply":
            reply_id = seg_data.get("id")
            reply_content = await self._get_reply_content(
                session=session,
                event=event,
                group_id=event.group_id,
                reply_id=reply_id,
            )
            return (
                self._wrap("System-Reply", reply_content),
                message_type,
            )

        if seg_type == "face":
            face_id = seg_data.get("id")
            return (
                self._wrap(
                    "System-QQFace",
                    "QQ表情",
                    {"qq_face_id": face_id},
                ),
                message_type,
            )

        if seg_type == "record":
            message_type = self._update_message_type(message_type, "aud")
            return (
                self._wrap(
                    "System-AudioPlaceholder",
                    "语音消息",
                    {"record_size": "", "record_duration": ""},
                ),
                message_type,
            )

        if seg_type == "file":
            message_type = self._update_message_type(message_type, "others")
            file_meta = self._extract_file_meta(seg_data)
            return (
                self._wrap(
                    "System-FilePlaceholder",
                    file_meta["content"],
                    {
                        "file_size": file_meta["size"],
                        "file_name": file_meta["name"],
                        "file_format": file_meta["format"],
                    },
                ),
                message_type,
            )

        if seg_type == "image":
            message_type = self._update_message_type(message_type, "img")
            image_service = ImageParsingService(session)
            parse_result = await image_service.parse_segment_sync(
                group_id=event.group_id,
                segment=segment,
            )
            return (
                self._wrap(
                    "System-Image",
                    parse_result.description,
                    {
                        "file_hash": parse_result.file_hash,
                        "url": parse_result.url,
                        "local_path": parse_result.local_path,
                        "desc": parse_result.description,
                        "parse_status": "ok" if parse_result.success else "failed",
                    },
                ),
                message_type,
            )

        if seg_type == "video":
            message_type = self._update_message_type(message_type, "vid")
            return (
                self._wrap("System-Other", str(segment), {"type": "video"}),
                message_type,
            )

        if seg_type == "forward":
            return (
                self._wrap("System-Other", "合并转发的消息", {"type": "forward"}),
                message_type,
            )

        if seg_type in {"xml", "json"}:
            raw = seg_data.get("data") or getattr(event, "raw_message", "") or ""
            raw = raw or str(segment)
            return (
                self._wrap("System-Unknown", raw, {"unknown_type": seg_type}),
                message_type,
            )

        return (
            self._wrap("System-Other", str(segment), {"type": seg_type}),
            message_type,
        )

    async def _get_reply_content(
        self,
        session: AsyncSession,
        event: GroupMessageEvent,
        group_id: int,
        reply_id: str | int | None,
    ) -> str:
        reply_event = getattr(event, "reply", None)
        if reply_event is not None:
            for attr_name in ("message", "raw_message", "message_text"):
                reply_value = getattr(reply_event, attr_name, None)
                if reply_value:
                    return str(reply_value)

        normalized_reply_id: str | None = None
        if reply_id is not None:
            normalized_reply_id = str(reply_id).strip() or None

        if normalized_reply_id is not None:
            try:
                message_service = GroupMessageService(session)
                reply_message = await message_service.get_message_by_onebot_message_id(
                    group_id=group_id,
                    onebot_message_id=normalized_reply_id,
                )
                if reply_message:
                    return (
                        reply_message.get("raw_message")
                        or reply_message.get("formatted_message")
                        or f"引用消息#{normalized_reply_id}"
                    )
            except Exception as exc:
                logger.debug(
                    "[message_converter] failed to resolve reply content",
                    extra={
                        "group_id": group_id,
                        "reply_id": normalized_reply_id,
                        "error": str(exc),
                    },
                )

        return f"引用消息#{reply_id}" if reply_id else "引用消息"

    def _extract_file_meta(self, data: dict) -> dict[str, str]:
        file_name = data.get("file") or data.get("name") or ""
        file_size = data.get("file_size") or data.get("size") or ""

        name_part = str(file_name)
        format_part = ""
        if "." in name_part:
            name_prefix, name_suffix = name_part.rsplit(".", 1)
            name_part = name_prefix
            format_part = name_suffix

        content = ""
        if name_part and format_part:
            content = f"{name_part}.{format_part}"
        elif name_part:
            content = name_part

        return {
            "size": self._attr_value(file_size),
            "name": self._attr_value(name_part),
            "format": self._attr_value(format_part),
            "content": content,
        }

    def _update_message_type(self, current: str, new_type: str) -> str:
        priority = {"text": 0, "others": 1, "img": 2, "aud": 2, "vid": 2}
        if new_type not in priority:
            return current
        if priority.get(new_type, 0) > priority.get(current, 0):
            return new_type
        return current

    def _wrap_message(
        self,
        content: str,
        user_id: int | None = None,
        display_name: str | None = None,
        timestamp: str | None = None,
    ) -> str:
        attrs = {
            "user_id": user_id,
            "display_name": display_name,
            "timestamp": timestamp,
        }
        return self._wrap("System-Message", content, attrs, escape_content=False)

    def _wrap(
        self,
        tag: str,
        content: str,
        attrs: dict[str, str | int | None] | None = None,
        escape_content: bool = True,
    ) -> str:
        attr_text = ""
        if attrs:
            pairs = []
            for key, value in attrs.items():
                pairs.append(f'{key}="{self._attr_value(value)}"')
            attr_text = " " + " ".join(pairs)

        safe_content = self._escape_text(content) if escape_content else content
        return f"<{tag}{attr_text}>{safe_content}</{tag}>"

    def _wrap_unknown(self, unknown_type: str, raw: str) -> str:
        content = raw or "无"
        return self._wrap("System-Unknown", content, {"unknown_type": unknown_type})

    def _attr_value(self, value: str | int | None) -> str:
        if value is None:
            return ""
        return self._escape_text(str(value))

    def _escape_text(self, value: str) -> str:
        return escape(value, quote=True)

    async def _resolve_sender_name(
        self,
        session: AsyncSession,
        event: GroupMessageEvent,
        name_cache: dict[int, str],
    ) -> str:
        group_id = getattr(event, "group_id", None)
        user_id = getattr(event, "user_id", None)
        if group_id is None or user_id is None:
            return "无"

        try:
            return await DisplayNameResolver.resolve(
                session=session,
                group_id=group_id,
                user_id=user_id,
                name_cache=name_cache,
            )
        except Exception as exc:
            logger.debug(
                "[message_converter] resolve sender name failed",
                extra={
                    "group_id": group_id,
                    "user_id": user_id,
                    "error": str(exc),
                },
            )
            return f"用户{user_id}"

    def _format_message_time(self, value: datetime | int | float | None) -> str:
        if value is None:
            return ""
        if isinstance(value, datetime):
            return normalize_china_time(value).strftime("%Y-%m-%d %H:%M:%S")
        return normalize_china_time(value).strftime("%Y-%m-%d %H:%M:%S")

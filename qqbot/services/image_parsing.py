from __future__ import annotations

import asyncio
import base64
import hashlib
import mimetypes
import re
import time
from dataclasses import dataclass
from html import escape
from pathlib import Path

import httpx
from langchain_core.messages import HumanMessage
from nonebot.adapters.onebot.v11 import MessageSegment
from sqlalchemy.ext.asyncio import AsyncSession

from qqbot.core.llm import create_llm
from qqbot.core.logging import get_logger
from qqbot.core.time import normalize_china_time
from qqbot.services.group_message import GroupMessageService
from qqbot.services.image_record import ImageRecordService

logger = get_logger(__name__)

IMAGE_CACHE_DIR = Path("./sqlite_data/images")
DEFAULT_FAILURE_DESC = "图片解析失败"
IMAGE_PARSE_LLM_TIMEOUT_SECONDS = 45.0
SYSTEM_IMAGE_TAG_RE = re.compile(r"<System-Image\s+([^>]*)>(.*?)</System-Image>")
ATTR_RE = re.compile(r'(\w+)="([^"]*)"')


@dataclass
class ImageParseResult:
    file_hash: str
    url: str | None
    local_path: str | None
    description: str
    success: bool


@dataclass
class ImageReference:
    file_hash: str
    timestamp: object | None = None


class ImageParsingService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.record_service = ImageRecordService(session)

    async def parse_segment_sync(
        self,
        group_id: int,
        segment: MessageSegment,
    ) -> ImageParseResult:
        url = self._extract_image_url(segment)
        fallback_hash = self._build_fallback_hash(segment)

        try:
            download_started_at = time.perf_counter()
            logger.info(
                "[image_parsing] stage0 download start",
                extra={"group_id": group_id, "image_url": url},
            )
            image_bytes = await self._download_image(url)
            logger.info(
                "[image_parsing] stage0 download done",
                extra={
                    "group_id": group_id,
                    "image_url": url,
                    "image_bytes": len(image_bytes),
                    "elapsed_ms": int((time.perf_counter() - download_started_at) * 1000),
                },
            )
            file_hash = hashlib.md5(image_bytes).hexdigest()
            local_path = self._persist_image(file_hash, image_bytes, url)
            record = await self.record_service.upsert_record(
                file_hash=file_hash,
                url=url,
                local_path=local_path,
            )

            context = await self._get_recent_context_before(group_id=group_id)
            description = await self._describe_image(
                image_bytes=image_bytes,
                context=context,
            )
            if description:
                record.description = description
                await self.session.flush()
                return ImageParseResult(
                    file_hash=file_hash,
                    url=url,
                    local_path=local_path,
                    description=description,
                    success=True,
                )

            return self._failure_result(
                file_hash=file_hash,
                url=url,
                local_path=local_path,
            )
        except Exception as exc:
            logger.warning(
                "[image_parsing] stage0 parse failed",
                extra={
                    "group_id": group_id,
                    "error": str(exc),
                    "image_url": url,
                },
            )
            file_hash = fallback_hash
            await self.record_service.upsert_record(
                file_hash=file_hash,
                url=url,
                local_path=None,
            )
            return self._failure_result(file_hash=file_hash, url=url, local_path=None)

    async def reparse_file_hashes(
        self,
        group_id: int,
        refs: list[ImageReference],
    ) -> dict[str, str]:
        descriptions: dict[str, str] = {}
        deduped: dict[str, ImageReference] = {}
        for ref in refs:
            deduped.setdefault(ref.file_hash, ref)

        for file_hash, ref in deduped.items():
            record = await self.record_service.get_by_hash(file_hash)
            if record is None or not record.local_path:
                logger.warning(
                    "[image_parsing] stage3 reparse skipped",
                    extra={
                        "group_id": group_id,
                        "file_hash": file_hash,
                        "reason": "missing record or local_path",
                    },
                )
                continue

            image_path = Path(record.local_path)
            if not image_path.exists():
                logger.warning(
                    "[image_parsing] stage3 reparse skipped",
                    extra={
                        "group_id": group_id,
                        "file_hash": file_hash,
                        "reason": "local file missing",
                        "local_path": record.local_path,
                    },
                )
                continue

            try:
                image_bytes = image_path.read_bytes()
                context = await self._get_recent_context_before(
                    group_id=group_id,
                    before_timestamp=ref.timestamp,
                )
                description = await self._describe_image(
                    image_bytes=image_bytes,
                    context=context,
                )
                if not description:
                    continue

                record.description = description
                descriptions[file_hash] = description
            except Exception as exc:
                logger.warning(
                    "[image_parsing] stage3 reparse failed",
                    extra={
                        "group_id": group_id,
                        "file_hash": file_hash,
                        "error": str(exc),
                    },
                )

        if descriptions:
            await self.session.flush()
        return descriptions

    async def refresh_image_descriptions_in_text(self, text: str) -> str:
        file_hashes = self.extract_image_hashes(text)
        if not file_hashes:
            return text

        records = await self.record_service.get_by_hashes(file_hashes)
        return self._refresh_text_with_records(text, records)

    async def refresh_multiple_texts(self, texts: list[str]) -> list[str]:
        if not texts:
            return []

        merged_hashes: list[str] = []
        for text in texts:
            merged_hashes.extend(self.extract_image_hashes(text))

        if not merged_hashes:
            return texts

        records = await self.record_service.get_by_hashes(list(dict.fromkeys(merged_hashes)))

        return [self._refresh_text_with_records(text, records) for text in texts]

    @staticmethod
    def _refresh_text_with_records(
        text: str,
        records: dict[str, object],
    ) -> str:
        def _replace(match: re.Match[str]) -> str:
            attrs_text = match.group(1)
            attrs = dict(ATTR_RE.findall(attrs_text))
            file_hash = attrs.get("file_hash", "")
            record = records.get(file_hash)
            if record is None or not getattr(record, "description", None):
                return match.group(0)

            description = getattr(record, "description")
            attrs["desc"] = description
            attrs_str = " ".join(
                f'{key}="{escape(str(value), quote=True)}"'
                for key, value in attrs.items()
            )
            safe_desc = escape(description, quote=True)
            return f"<System-Image {attrs_str}>{safe_desc}</System-Image>"

        return SYSTEM_IMAGE_TAG_RE.sub(_replace, text)

    @staticmethod
    def extract_image_hashes(text: str) -> list[str]:
        hashes: list[str] = []
        for attrs_text, _ in SYSTEM_IMAGE_TAG_RE.findall(text):
            attrs = dict(ATTR_RE.findall(attrs_text))
            file_hash = attrs.get("file_hash")
            if file_hash:
                hashes.append(file_hash)
        return hashes

    @staticmethod
    def extract_refs_from_formatted_message(
        formatted_message: str,
        timestamp: object | None = None,
    ) -> list[ImageReference]:
        return [
            ImageReference(file_hash=file_hash, timestamp=timestamp)
            for file_hash in ImageParsingService.extract_image_hashes(formatted_message)
        ]

    def _failure_result(
        self,
        file_hash: str,
        url: str | None,
        local_path: str | None,
    ) -> ImageParseResult:
        return ImageParseResult(
            file_hash=file_hash,
            url=url,
            local_path=local_path,
            description=DEFAULT_FAILURE_DESC,
            success=False,
        )

    async def _get_recent_context_before(
        self,
        group_id: int,
        before_timestamp: object | None = None,
        limit: int = 10,
    ) -> str:
        message_service = GroupMessageService(self.session)
        if before_timestamp is None:
            messages = await message_service.get_recent_messages(group_id=group_id, limit=limit)
        else:
            messages = await message_service.get_messages_before_timestamp(
                group_id=group_id,
                before_timestamp=before_timestamp,
                limit=limit,
            )
        if not messages:
            return "（暂无历史上下文）"

        parts = [
            message.get("formatted_message") or message.get("raw_message") or ""
            for message in messages
        ]
        return "\n".join(part for part in parts if part)

    async def _describe_image(self, image_bytes: bytes, context: str) -> str | None:
        llm = await create_llm(temperature=0.2)
        if llm is None:
            logger.warning("[image_parsing] image llm unavailable")
            return None

        image_data_url = self._build_image_data_url(image_bytes)
        prompt = (
            "你是群聊图片理解工具。请结合这张图片之前的聊天上下文，"
            "输出一段简洁、适合放入群聊上下文的中文图片描述。"
            "只描述图片中对群聊理解有帮助的内容，不要输出多余前缀。\n\n"
            f"【图片前的最近聊天记录】\n{context}"
        )
        message = HumanMessage(
            content=[
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": image_data_url}},
            ]
        )
        started_at = time.perf_counter()
        logger.info(
            "[image_parsing] vision request start",
            extra={
                "image_bytes": len(image_bytes),
                "context_chars": len(context),
                "timeout_seconds": IMAGE_PARSE_LLM_TIMEOUT_SECONDS,
            },
        )
        try:
            response = await asyncio.wait_for(
                llm.ainvoke([message]),
                timeout=IMAGE_PARSE_LLM_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            logger.error(
                "[image_parsing] vision request timeout",
                extra={
                    "image_bytes": len(image_bytes),
                    "context_chars": len(context),
                    "timeout_seconds": IMAGE_PARSE_LLM_TIMEOUT_SECONDS,
                },
            )
            return None

        logger.info(
            "[image_parsing] vision request done",
            extra={
                "image_bytes": len(image_bytes),
                "context_chars": len(context),
                "elapsed_ms": int((time.perf_counter() - started_at) * 1000),
                "response_type": type(response.content).__name__,
            },
        )
        return self._extract_text_response(response.content)

    @staticmethod
    def _extract_text_response(content: object) -> str | None:
        if isinstance(content, str):
            normalized = content.strip()
            return normalized or None

        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item.strip())
                    continue
                if isinstance(item, dict):
                    text = item.get("text")
                    if isinstance(text, str) and text.strip():
                        parts.append(text.strip())
            merged = "\n".join(part for part in parts if part)
            return merged or None

        return None

    @staticmethod
    def _extract_image_url(segment: MessageSegment) -> str | None:
        data = segment.data
        for key in ("url", "file", "src"):
            value = data.get(key)
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                return value
        return None

    @staticmethod
    def _build_fallback_hash(segment: MessageSegment) -> str:
        data = segment.data
        raw = "|".join(str(data.get(key, "")) for key in sorted(data)) or str(segment)
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    async def _download_image(self, url: str | None) -> bytes:
        if not url:
            raise ValueError("missing image url")

        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.content

    def _persist_image(self, file_hash: str, image_bytes: bytes, url: str | None) -> str:
        IMAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        suffix = self._guess_suffix(url, image_bytes)
        image_path = IMAGE_CACHE_DIR / f"{file_hash}{suffix}"
        image_path.write_bytes(image_bytes)
        return image_path.as_posix()

    @staticmethod
    def _guess_suffix(url: str | None, image_bytes: bytes) -> str:
        guessed = None
        if url:
            guessed = mimetypes.guess_type(url)[0]
        if guessed:
            suffix = mimetypes.guess_extension(guessed)
            if suffix:
                return suffix

        if image_bytes.startswith(b"\x89PNG"):
            return ".png"
        if image_bytes.startswith(b"\xff\xd8"):
            return ".jpg"
        if image_bytes.startswith((b"GIF87a", b"GIF89a")):
            return ".gif"
        if image_bytes.startswith(b"RIFF") and b"WEBP" in image_bytes[:16]:
            return ".webp"
        return ".img"

    @staticmethod
    def _build_image_data_url(image_bytes: bytes) -> str:
        mime_type = "application/octet-stream"
        if image_bytes.startswith(b"\x89PNG"):
            mime_type = "image/png"
        elif image_bytes.startswith(b"\xff\xd8"):
            mime_type = "image/jpeg"
        elif image_bytes.startswith((b"GIF87a", b"GIF89a")):
            mime_type = "image/gif"
        elif image_bytes.startswith(b"RIFF") and b"WEBP" in image_bytes[:16]:
            mime_type = "image/webp"
        encoded = base64.b64encode(image_bytes).decode("utf-8")
        return f"data:{mime_type};base64,{encoded}"

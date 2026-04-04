from __future__ import annotations

import asyncio
import base64
import hashlib
import mimetypes
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from langchain_core.messages import HumanMessage
from nonebot.adapters.onebot.v11 import MessageSegment
from sqlalchemy.ext.asyncio import AsyncSession

from qqbot.core.llm import create_llm
from qqbot.core.logging import get_logger
from qqbot.services.tool_manager import ToolCallResult, ToolManager

logger = get_logger(__name__)

IMAGE_CACHE_DIR = Path("./runtime_data/images")
DEFAULT_FAILURE_DESC = "图片解析失败"
IMAGE_PARSE_LLM_TIMEOUT_SECONDS = 45.0
SYSTEM_IMAGE_TAG_RE = re.compile(r"<System-Image\s+([^>]*)>.*?</System-Image>")
ATTR_RE = re.compile(r'(\w+)="([^"]*)"')


@dataclass
class ImageParseResult:
    file_hash: str
    description: str


class ImageParsingService:
    def __init__(self, session: AsyncSession | None = None) -> None:
        self.session = session
        self.tool_manager = ToolManager(session) if session is not None else None

    async def parse_segment_sync(
        self,
        group_id: int,
        segment: MessageSegment,
        msg_hash: str,
    ) -> ImageParseResult:
        _ = msg_hash
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
                "[image_parsing] stage0 image cached",
                extra={
                    "group_id": group_id,
                    "image_url": url,
                    "image_bytes": len(image_bytes),
                    "elapsed_ms": int((time.perf_counter() - download_started_at) * 1000),
                },
            )
            file_hash = hashlib.md5(image_bytes).hexdigest()
            self._persist_image(file_hash, image_bytes, url)
            try:
                description = await self._generate_initial_image_summary(image_bytes)
            except Exception as exc:
                logger.warning(
                    "[image_parsing] stage0 initial description failed",
                    extra={
                        "group_id": group_id,
                        "file_hash": file_hash,
                        "error": str(exc),
                    },
                )
                description = DEFAULT_FAILURE_DESC
            return ImageParseResult(file_hash=file_hash, description=description)
        except Exception as exc:
            logger.warning(
                "[image_parsing] stage0 image cache failed",
                extra={
                    "group_id": group_id,
                    "error": str(exc),
                    "image_url": url,
                },
            )
            return ImageParseResult(
                file_hash=fallback_hash,
                description=DEFAULT_FAILURE_DESC,
            )

    async def execute_image_parse_by_hash(
        self,
        *,
        msg_hash: str,
        file_hash: str,
        context: str,
        reuse_existing: bool = False,
    ) -> ToolCallResult:
        if self.tool_manager is None:
            raise RuntimeError("ImageParsingService requires a session for tool execution")

        return await self.tool_manager.execute_image_parse(
            msg_hash=msg_hash,
            file_hash=file_hash,
            generator=lambda: self._generate_image_summary_from_hash(
                file_hash=file_hash,
                context=context,
            ),
            reuse_existing=reuse_existing,
        )

    def build_layer3_image_inputs(self, file_hashes: list[str]) -> list[dict[str, Any]]:
        image_inputs: list[dict[str, Any]] = []
        seen_hashes: set[str] = set()

        for file_hash in file_hashes:
            normalized_hash = file_hash.strip()
            if not normalized_hash or normalized_hash in seen_hashes:
                continue
            seen_hashes.add(normalized_hash)

            image_data_url = self.get_image_data_url(normalized_hash)
            if image_data_url is None:
                logger.warning(
                    "[image_parsing] cached image missing for layer3",
                    extra={"file_hash": normalized_hash},
                )
                continue

            image_inputs.extend(
                [
                    {
                        "type": "text",
                        "text": f"以下图片对应的 file_hash 是 {normalized_hash}。",
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": image_data_url},
                    },
                ]
            )

        return image_inputs

    @staticmethod
    def extract_image_hashes(text: str) -> list[str]:
        hashes: list[str] = []
        for attrs_text in SYSTEM_IMAGE_TAG_RE.findall(text):
            attrs = dict(ATTR_RE.findall(attrs_text))
            file_hash = attrs.get("file_hash")
            if file_hash:
                hashes.append(file_hash)
        return hashes

    async def _generate_image_summary(self, image_bytes: bytes, context: str) -> str:
        description = await self._describe_image(image_bytes=image_bytes, context=context)
        return description or DEFAULT_FAILURE_DESC

    async def _generate_initial_image_summary(self, image_bytes: bytes) -> str:
        description = await self._describe_image(image_bytes=image_bytes, context="")
        return description or DEFAULT_FAILURE_DESC

    async def _build_failure_summary(self) -> str:
        return DEFAULT_FAILURE_DESC

    async def _generate_image_summary_from_hash(self, *, file_hash: str, context: str) -> str:
        image_path = self._find_cached_image_path(file_hash)
        if image_path is None:
            logger.warning(
                "[image_parsing] cached image not found",
                extra={"file_hash": file_hash},
            )
            return await self._build_failure_summary()

        try:
            image_bytes = image_path.read_bytes()
        except Exception as exc:
            logger.warning(
                "[image_parsing] failed to read cached image",
                extra={"file_hash": file_hash, "error": str(exc)},
            )
            return await self._build_failure_summary()

        return await self._generate_image_summary(image_bytes=image_bytes, context=context)

    async def _describe_image(self, image_bytes: bytes, context: str) -> str | None:
        llm = await create_llm(temperature=0.2)
        if llm is None:
            logger.warning("[image_parsing] image llm unavailable")
            return None

        image_data_url = self._build_image_data_url(image_bytes)
        normalized_context = context.strip()
        if normalized_context:
            prompt = (
                "你是群聊图片理解工具。请结合这张图片所在的当前群聊上下文和当前回复任务，"
                "输出一段简洁、适合放入群聊上下文的中文图片描述。"
                "只描述图片中对群聊理解有帮助的内容，不要输出多余前缀。\n\n"
                f"【当前上下文】\n{normalized_context}"
            )
        else:
            prompt = (
                "你是群聊图片理解工具。当前没有额外上下文，请只根据图片本身输出一段简洁中文描述，"
                "供后续群聊理解使用。"
                "优先概括主体、动作、场景、明显文字或梗图关键信息，不要输出多余前缀。"
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

    def _persist_image(self, file_hash: str, image_bytes: bytes, url: str | None) -> None:
        IMAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        suffix = self._guess_suffix(url, image_bytes)
        image_path = IMAGE_CACHE_DIR / f"{file_hash}{suffix}"
        image_path.write_bytes(image_bytes)

    def get_image_data_url(self, file_hash: str) -> str | None:
        image_path = self._find_cached_image_path(file_hash)
        if image_path is None:
            return None
        try:
            image_bytes = image_path.read_bytes()
        except Exception as exc:
            logger.warning(
                "[image_parsing] failed to load cached image for layer3",
                extra={"file_hash": file_hash, "error": str(exc)},
            )
            return None
        return self._build_image_data_url(image_bytes)

    @staticmethod
    def _find_cached_image_path(file_hash: str) -> Path | None:
        if not IMAGE_CACHE_DIR.exists():
            return None

        candidates = sorted(IMAGE_CACHE_DIR.glob(f"{file_hash}.*"))
        return candidates[0] if candidates else None

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

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from qqbot.core.ids import new_call_hash
from qqbot.core.logging import get_logger
from qqbot.services.tool_call_record import ToolCallRecordService, build_system_tool_call_xml
from qqbot.services.web_search import WebSearchService

logger = get_logger(__name__)


@dataclass
class ToolCallResult:
    call_hash: str
    tool_name: str
    input_data: str
    output_data: str
    is_newly_generated: bool

    def to_system_xml(self) -> str:
        return build_system_tool_call_xml(
            call_hash=self.call_hash,
            tool_name=self.tool_name,
            input_data=self.input_data,
            output_data=self.output_data,
        )


class ToolManager:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.record_service = ToolCallRecordService(session)
        self.web_search_service = WebSearchService()

    async def execute_image_parse(
        self,
        *,
        msg_hash: str,
        file_hash: str,
        generator: Callable[[], Awaitable[str]],
        reuse_existing: bool = True,
    ) -> ToolCallResult:
        return await self._execute_tool(
            msg_hash=msg_hash,
            tool_name="image_parse",
            input_data=file_hash,
            generator=generator,
            reuse_existing=reuse_existing,
        )

    async def execute_web_search(
        self,
        *,
        msg_hash: str,
        query: str,
    ) -> ToolCallResult:
        return await self._execute_tool(
            msg_hash=msg_hash,
            tool_name="web_search",
            input_data=query,
            generator=lambda: self.web_search_service.search(query),
            reuse_existing=False,
        )

    async def execute_web_crawl(
        self,
        *,
        msg_hash: str,
        url: str,
    ) -> ToolCallResult:
        return await self._execute_tool(
            msg_hash=msg_hash,
            tool_name="web_crawl",
            input_data=url,
            generator=lambda: self.web_search_service.crawl(url),
            reuse_existing=False,
        )

    async def _execute_tool(
        self,
        *,
        msg_hash: str,
        tool_name: str,
        input_data: str,
        generator: Callable[[], Awaitable[str]],
        reuse_existing: bool,
    ) -> ToolCallResult:
        logger.info(
            "[tool_manager] tool execution started",
            extra={
                "tool": tool_name,
                "msg_hash": msg_hash,
                "input_preview": input_data[:120],
                "reuse_existing": reuse_existing,
            },
        )
        if reuse_existing:
            record = await self.record_service.get_reusable_record(
                tool_name=tool_name,
                input_data=input_data,
            )
            if record is not None:
                reused_result = ToolCallResult(
                    call_hash=new_call_hash(),
                    tool_name=record.tool_name,
                    input_data=record.input_data,
                    output_data=record.output_data,
                    is_newly_generated=False,
                )
                await self.record_service.create_record(
                    call_hash=reused_result.call_hash,
                    msg_hash=msg_hash,
                    tool_name=reused_result.tool_name,
                    input_data=reused_result.input_data,
                    output_data=reused_result.output_data,
                )
                logger.info(
                    "[tool_manager] reused existing tool result",
                    extra={
                        "tool": tool_name,
                        "msg_hash": msg_hash,
                        "source_call_hash": record.call_hash,
                        "call_hash": reused_result.call_hash,
                    },
                )
                return reused_result

        output_data = await generator()
        result = ToolCallResult(
            call_hash=new_call_hash(),
            tool_name=tool_name,
            input_data=input_data,
            output_data=output_data,
            is_newly_generated=True,
        )
        await self.record_service.create_record(
            call_hash=result.call_hash,
            msg_hash=msg_hash,
            tool_name=result.tool_name,
            input_data=result.input_data,
            output_data=result.output_data,
        )
        logger.info(
            "[tool_manager] tool execution finished",
            extra={
                "tool": result.tool_name,
                "msg_hash": msg_hash,
                "call_hash": result.call_hash,
                "output_length": len(result.output_data),
                "is_newly_generated": result.is_newly_generated,
            },
        )
        return result

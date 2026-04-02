from __future__ import annotations

from collections import defaultdict
from html import escape

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qqbot.models.tool_call import ToolCallRecord


def build_system_tool_call_xml(
    *,
    call_hash: str,
    tool_name: str,
    input_data: str,
    output_data: str,
) -> str:
    safe_output = escape(output_data, quote=True)
    return (
        f'<System-ToolCall call_hash="{escape(call_hash, quote=True)}" '
        f'tool="{escape(tool_name, quote=True)}" '
        f'input="{escape(input_data, quote=True)}">'
        f"{safe_output}</System-ToolCall>"
    )


class ToolCallRecordService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_reusable_record(
        self,
        *,
        tool_name: str,
        input_data: str,
    ) -> ToolCallRecord | None:
        result = await self.session.execute(
            select(ToolCallRecord)
            .where(ToolCallRecord.tool_name == tool_name)
            .where(ToolCallRecord.input_data == input_data)
            .order_by(ToolCallRecord.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def get_records_by_call_hashes(
        self,
        call_hashes: list[str],
    ) -> dict[str, ToolCallRecord]:
        if not call_hashes:
            return {}

        result = await self.session.execute(
            select(ToolCallRecord).where(ToolCallRecord.call_hash.in_(call_hashes))
        )
        records = result.scalars().all()
        return {record.call_hash: record for record in records}

    async def create_record(
        self,
        *,
        call_hash: str,
        msg_hash: str,
        tool_name: str,
        input_data: str,
        output_data: str,
    ) -> ToolCallRecord:
        record = ToolCallRecord(
            call_hash=call_hash,
            msg_hash=msg_hash,
            tool_name=tool_name,
            input_data=input_data,
            output_data=output_data,
        )
        self.session.add(record)
        await self.session.flush()
        return record

    async def get_records_by_msg_hashes(
        self,
        msg_hashes: list[str],
    ) -> dict[str, list[ToolCallRecord]]:
        if not msg_hashes:
            return {}

        result = await self.session.execute(
            select(ToolCallRecord)
            .where(ToolCallRecord.msg_hash.in_(msg_hashes))
            .order_by(ToolCallRecord.created_at.asc(), ToolCallRecord.call_hash.asc())
        )
        records = result.scalars().all()
        grouped: dict[str, list[ToolCallRecord]] = defaultdict(list)
        for record in records:
            grouped[record.msg_hash].append(record)
        return grouped

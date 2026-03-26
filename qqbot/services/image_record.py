from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qqbot.models.image import ImageRecord


class ImageRecordService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_hash(self, file_hash: str) -> ImageRecord | None:
        result = await self.session.execute(
            select(ImageRecord).where(ImageRecord.file_hash == file_hash)
        )
        return result.scalar_one_or_none()

    async def get_by_hashes(self, file_hashes: list[str]) -> dict[str, ImageRecord]:
        if not file_hashes:
            return {}

        result = await self.session.execute(
            select(ImageRecord).where(ImageRecord.file_hash.in_(file_hashes))
        )
        records = result.scalars().all()
        return {record.file_hash: record for record in records}

    async def upsert_record(
        self,
        file_hash: str,
        url: str | None,
        local_path: str | None,
        description: str | None = None,
    ) -> ImageRecord:
        record = await self.get_by_hash(file_hash)
        if record is None:
            record = ImageRecord(
                file_hash=file_hash,
                url=url,
                local_path=local_path,
                description=description,
            )
            self.session.add(record)
            await self.session.flush()
            return record

        if url:
            record.url = url
        if local_path:
            record.local_path = local_path
        if description is not None:
            record.description = description
        await self.session.flush()
        return record

    async def update_description(self, file_hash: str, description: str) -> ImageRecord | None:
        record = await self.get_by_hash(file_hash)
        if record is None:
            return None

        record.description = description
        await self.session.flush()
        return record

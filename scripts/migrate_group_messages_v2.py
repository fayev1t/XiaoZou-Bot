"""Migrate per-group message tables to v2 schema with formatted XML."""

from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from nonebot.adapters.onebot.v11 import Message
from sqlalchemy import select, text

from qqbot.core.database import AsyncSessionLocal
from qqbot.models import Group
from qqbot.services.message_converter import MessageConverter

logger = logging.getLogger(__name__)


@dataclass
class LegacyEvent:
    """Minimal event shim for conversion."""

    group_id: int
    user_id: int | None
    message: Message
    raw_message: str
    time: datetime | None = None


async def _create_v2_table(session: Any, table_name: str) -> None:
    await session.execute(text(f"""
        CREATE TABLE IF NOT EXISTS {table_name} (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            raw_message TEXT,
            formatted_message TEXT,
            is_recalled BOOLEAN DEFAULT FALSE,
            "timestamp" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """))
    await session.execute(text(f"""
        CREATE INDEX IF NOT EXISTS idx_{table_name}_user_id
        ON {table_name}(user_id)
    """))
    await session.execute(text(f"""
        CREATE INDEX IF NOT EXISTS idx_{table_name}_is_recalled
        ON {table_name}(is_recalled)
    """))
    await session.execute(text(f"""
        CREATE INDEX IF NOT EXISTS idx_{table_name}_timestamp
        ON {table_name}("timestamp")
    """))


async def _table_count(session: Any, table_name: str) -> int:
    result = await session.execute(text(f"SELECT COUNT(*) FROM {table_name}"))
    return int(result.scalar() or 0)


async def _migrate_group(
    session: Any,
    group: Group,
    converter: MessageConverter,
    batch_size: int,
) -> None:
    old_table = group.table_name
    new_table = f"group_messages_v2_{group.group_id}"

    await _create_v2_table(session, new_table)
    await session.commit()

    existing = await _table_count(session, new_table)
    if existing > 0:
        await _fill_missing_formatted(
            session=session,
            group_id=group.group_id,
            table_name=new_table,
            converter=converter,
            batch_size=batch_size,
        )
        return

    offset = 0
    total = 0
    while True:
        result = await session.execute(text(f"""
            SELECT id, user_id, message_content, is_recalled, "timestamp"
            FROM {old_table}
            ORDER BY id
            LIMIT :limit OFFSET :offset
        """), {"limit": batch_size, "offset": offset})
        rows = result.fetchall()
        if not rows:
            break

        for row in rows:
            raw_message = row.message_content or ""
            message = Message(raw_message)
            event = LegacyEvent(
                group_id=group.group_id,
                user_id=row.user_id,
                message=message,
                raw_message=raw_message,
                time=row.timestamp,
            )
            converted = await converter.convert_event(session, event)

            await session.execute(text(f"""
                INSERT INTO {new_table} (
                    user_id,
                    raw_message,
                    formatted_message,
                    is_recalled,
                    "timestamp"
                ) VALUES (
                    :user_id,
                    :raw_message,
                    :formatted_message,
                    :is_recalled,
                    :timestamp
                )
            """), {
                "user_id": row.user_id,
                "raw_message": raw_message,
                "formatted_message": converted.content,
                "is_recalled": row.is_recalled,
                "timestamp": row.timestamp,
            })

        await session.commit()
        batch_count = len(rows)
        total += batch_count
        offset += batch_size
        logger.info(
            "[migrate] group=%s migrated %s rows",
            group.group_id,
            total,
        )


async def _fill_missing_formatted(
    session: Any,
    group_id: int,
    table_name: str,
    converter: MessageConverter,
    batch_size: int,
) -> None:
    total = 0
    while True:
        result = await session.execute(text(f"""
            SELECT id, user_id, raw_message, "timestamp"
            FROM {table_name}
            WHERE formatted_message IS NULL
            ORDER BY id
            LIMIT :limit
        """), {"limit": batch_size})
        rows = result.fetchall()
        if not rows:
            break

        for row in rows:
            raw_message = row.raw_message or ""
            message = Message(raw_message)
            event = LegacyEvent(
                group_id=group_id,
                user_id=row.user_id,
                message=message,
                raw_message=raw_message,
                time=row.timestamp,
            )
            converted = await converter.convert_event(session, event)

            await session.execute(text(f"""
                UPDATE {table_name}
                SET formatted_message = :formatted_message
                WHERE id = :id
            """), {"formatted_message": converted.content, "id": row.id})

        await session.commit()
        total += len(rows)
        logger.info(
            "[migrate] group=%s filled %s rows",
            group_id,
            total,
        )


async def migrate(batch_size: int, switch_tables: bool) -> None:
    converter = MessageConverter()

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Group))
        groups = result.scalars().all()

        if not groups:
            logger.info("[migrate] no groups found")
            return

        for group in groups:
            await _migrate_group(session, group, converter, batch_size)

        if switch_tables:
            await session.execute(text("""
                UPDATE groups
                SET table_name = format('group_messages_v2_%s', group_id)
            """))
            await session.commit()
            logger.info("[migrate] groups.table_name switched to v2 tables")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate group messages to v2 schema with formatted XML",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=200,
        help="Rows per batch (default: 200)",
    )
    parser.add_argument(
        "--switch",
        action="store_true",
        help="Switch groups.table_name to v2 tables after migration",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    asyncio.run(migrate(args.batch_size, args.switch))


if __name__ == "__main__":
    main()

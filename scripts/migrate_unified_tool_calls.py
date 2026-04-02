from __future__ import annotations

import asyncio
import re
from uuid import uuid4

from sqlalchemy import text

from qqbot.core.database import engine

SYSTEM_MESSAGE_OPEN_RE = re.compile(r"<System-Message\b([^>]*)>")
SYSTEM_IMAGE_RE = re.compile(r"<System-Image\s+([^>]*)>.*?</System-Image>")
ATTR_RE = re.compile(r'(\w+)="([^"]*)"')


def _new_hash() -> str:
    return uuid4().hex


def _inject_msg_hash(formatted_message: str, msg_hash: str) -> str:
    match = SYSTEM_MESSAGE_OPEN_RE.search(formatted_message)
    if match is None:
        return formatted_message
    attrs = match.group(1)
    if 'msg_hash="' in attrs:
        return formatted_message
    replacement = f'<System-Message msg_hash="{msg_hash}"{attrs}>'
    return (
        formatted_message[: match.start()]
        + replacement
        + formatted_message[match.end() :]
    )


def _extract_image_hashes(formatted_message: str) -> list[str]:
    image_hashes: list[str] = []
    for attrs_text in SYSTEM_IMAGE_RE.findall(formatted_message):
        attrs = dict(ATTR_RE.findall(attrs_text))
        file_hash = attrs.get("file_hash")
        if file_hash:
            image_hashes.append(file_hash)
    return image_hashes


async def _ensure_tool_tables(conn) -> None:
    await conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS tool_call_records (
                call_hash VARCHAR(64) PRIMARY KEY,
                msg_hash VARCHAR(64) NOT NULL,
                tool_name VARCHAR(64) NOT NULL,
                input_data TEXT NOT NULL,
                output_data TEXT NOT NULL,
                created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )
    await conn.execute(
        text(
            "ALTER TABLE tool_call_records ADD COLUMN IF NOT EXISTS msg_hash VARCHAR(64)"
        )
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS idx_tool_call_records_lookup ON tool_call_records(tool_name, input_data)"
        )
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS idx_tool_call_records_msg_hash ON tool_call_records(msg_hash)"
        )
    )
    await conn.execute(
        text(
            "ALTER TABLE tool_call_records ALTER COLUMN created_at SET DEFAULT CURRENT_TIMESTAMP"
        )
    )


async def _list_message_tables(conn) -> list[str]:
    result = await conn.execute(
        text(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name LIKE 'group_messages_v2_%'
            ORDER BY table_name
            """
        )
    )
    return [row[0] for row in result.fetchall()]


async def _ensure_msg_hash_column(conn, table_name: str) -> None:
    await conn.execute(
        text(f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS msg_hash VARCHAR(64)")
    )
    result = await conn.execute(text(f'SELECT id FROM {table_name} WHERE msg_hash IS NULL ORDER BY id'))
    ids = [row[0] for row in result.fetchall()]
    for local_id in ids:
        await conn.execute(
            text(f"UPDATE {table_name} SET msg_hash = :msg_hash WHERE id = :local_id"),
            {"msg_hash": _new_hash(), "local_id": local_id},
        )
    await conn.execute(text(f"ALTER TABLE {table_name} ALTER COLUMN msg_hash SET NOT NULL"))
    await conn.execute(
        text(f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{table_name}_msg_hash ON {table_name}(msg_hash)")
    )


async def _rewrite_formatted_messages(conn, table_name: str) -> None:
    result = await conn.execute(
        text(f"SELECT id, msg_hash, formatted_message FROM {table_name} WHERE formatted_message IS NOT NULL")
    )
    rows = result.fetchall()
    for local_id, msg_hash, formatted_message in rows:
        updated = _inject_msg_hash(formatted_message, msg_hash)
        if updated == formatted_message:
            continue
        await conn.execute(
            text(f"UPDATE {table_name} SET formatted_message = :formatted_message WHERE id = :local_id"),
            {"formatted_message": updated, "local_id": local_id},
        )


async def _backfill_tool_call_msg_hashes(conn) -> None:
    links_table_exists = await conn.execute(
        text(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name = 'message_tool_calls'
            )
            """
        )
    )
    if not links_table_exists.scalar():
        return

    result = await conn.execute(
        text(
            """
            SELECT mtc.msg_hash,
                   mtc.created_at,
                   tcr.call_hash,
                   tcr.tool_name,
                   tcr.input_data,
                   tcr.output_data,
                   tcr.created_at
            FROM message_tool_calls mtc
            JOIN tool_call_records tcr ON tcr.call_hash = mtc.call_hash
            ORDER BY tcr.call_hash ASC, mtc.created_at ASC, mtc.msg_hash ASC
            """
        )
    )
    grouped_rows: dict[str, list[tuple[str, object, str, str, str, str, object]]] = {}
    for row in result.fetchall():
        msg_hash, link_created_at, call_hash, tool_name, input_data, output_data, record_created_at = row
        grouped_rows.setdefault(call_hash, []).append(
            (
                msg_hash,
                link_created_at,
                call_hash,
                tool_name,
                input_data,
                output_data,
                record_created_at,
            )
        )

    for call_hash, rows in grouped_rows.items():
        first_msg_hash, _, _, _, _, _, _ = rows[0]
        await conn.execute(
            text(
                "UPDATE tool_call_records SET msg_hash = :msg_hash WHERE call_hash = :call_hash"
            ),
            {"msg_hash": first_msg_hash, "call_hash": call_hash},
        )
        for msg_hash, link_created_at, _, tool_name, input_data, output_data, record_created_at in rows[1:]:
            await conn.execute(
                text(
                    """
                    INSERT INTO tool_call_records (
                        call_hash,
                        msg_hash,
                        tool_name,
                        input_data,
                        output_data,
                        created_at
                    )
                    VALUES (
                        :call_hash,
                        :msg_hash,
                        :tool_name,
                        :input_data,
                        :output_data,
                        :created_at
                    )
                    """
                ),
                {
                    "call_hash": _new_hash(),
                    "msg_hash": msg_hash,
                    "tool_name": tool_name,
                    "input_data": input_data,
                    "output_data": output_data,
                    "created_at": link_created_at or record_created_at,
                },
            )


async def _backfill_image_tool_calls(conn, table_names: list[str]) -> None:
    image_records_exists = await conn.execute(
        text(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name = 'image_records'
            )
            """
        )
    )
    if not image_records_exists.scalar():
        return

    result = await conn.execute(text("SELECT file_hash, description FROM image_records ORDER BY file_hash"))
    description_by_file_hash = {
        file_hash: description or "图片解析失败" for file_hash, description in result.fetchall()
    }

    for table_name in table_names:
        result = await conn.execute(
            text(f"SELECT msg_hash, formatted_message FROM {table_name} WHERE formatted_message IS NOT NULL")
        )
        for msg_hash, formatted_message in result.fetchall():
            for file_hash in _extract_image_hashes(formatted_message):
                output_data = description_by_file_hash.get(file_hash)
                if output_data is None:
                    continue
                existing = await conn.execute(
                    text(
                        """
                        SELECT call_hash
                        FROM tool_call_records
                        WHERE msg_hash = :msg_hash
                          AND tool_name = 'image_parse'
                          AND input_data = :input_data
                        LIMIT 1
                        """
                    ),
                    {"msg_hash": msg_hash, "input_data": file_hash},
                )
                if existing.scalar():
                    continue
                await conn.execute(
                    text(
                        """
                        INSERT INTO tool_call_records (
                            call_hash,
                            msg_hash,
                            tool_name,
                            input_data,
                            output_data,
                            created_at
                        )
                        VALUES (
                            :call_hash,
                            :msg_hash,
                            'image_parse',
                            :input_data,
                            :output_data,
                            CURRENT_TIMESTAMP
                        )
                        """
                    ),
                    {
                        "call_hash": _new_hash(),
                        "msg_hash": msg_hash,
                        "input_data": file_hash,
                        "output_data": output_data,
                    },
                )


async def _finalize_tool_tables(conn) -> None:
    await conn.execute(
        text("DELETE FROM tool_call_records WHERE msg_hash IS NULL")
    )
    await conn.execute(
        text("ALTER TABLE tool_call_records ALTER COLUMN msg_hash SET NOT NULL")
    )
    await conn.execute(text("DROP TABLE IF EXISTS message_tool_calls"))


async def main() -> None:
    async with engine.begin() as conn:
        await _ensure_tool_tables(conn)
        table_names = await _list_message_tables(conn)
        for table_name in table_names:
            await _ensure_msg_hash_column(conn, table_name)
            await _rewrite_formatted_messages(conn, table_name)
        await _backfill_tool_call_msg_hashes(conn)
        await _backfill_image_tool_calls(conn, table_names)
        await _finalize_tool_tables(conn)


if __name__ == "__main__":
    asyncio.run(main())

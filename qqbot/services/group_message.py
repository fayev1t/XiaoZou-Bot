"""Group message management service."""

from datetime import datetime

from sqlalchemy import (
    select,
    text,
)
from sqlalchemy.ext.asyncio import AsyncSession

from qqbot.core.database import is_sqlite_backend
from qqbot.core.time import normalize_china_time
from qqbot.models import Group


class GroupMessageService:
    """Service for managing per-group message tables (v2)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def get_messages_table_name(group_id: int) -> str:
        """Get the table name for a group's messages."""
        return f"group_messages_v2_{group_id}"

    async def _get_table_name(self, group_id: int) -> str:
        result = await self.session.execute(
            select(Group).where(Group.group_id == group_id)
        )
        group = result.scalar_one_or_none()

        if not group:
            raise ValueError(f"Group {group_id} not found")

        return group.table_name

    async def save_message(
        self,
        group_id: int,
        user_id: int,
        onebot_message_id: str | None,
        raw_message: str | None,
        formatted_message: str | None,
        timestamp: datetime | int | float | None = None,
        is_recalled: bool = False,
    ) -> int:
        """Save a message to group message table."""
        table_name = await self._get_table_name(group_id)

        params = {
            "user_id": user_id,
            "onebot_message_id": onebot_message_id,
            "raw_message": raw_message,
            "formatted_message": formatted_message,
            "timestamp": normalize_china_time(timestamp),
            "is_recalled": is_recalled,
        }

        if is_sqlite_backend():
            sql = text(f"""
                INSERT INTO {table_name}
                (user_id, onebot_message_id, raw_message, formatted_message, "timestamp", is_recalled)
                VALUES (
                    :user_id,
                    :onebot_message_id,
                    :raw_message,
                    :formatted_message,
                    :timestamp,
                    :is_recalled
                )
            """)
            result = await self.session.execute(sql, params)
            if result.lastrowid is not None:
                return int(result.lastrowid)

            fallback = await self.session.execute(
                text("SELECT last_insert_rowid()")
            )
            saved_id = fallback.scalar()
            return int(saved_id) if saved_id is not None else 0

        sql = text(f"""
            INSERT INTO {table_name}
            (user_id, onebot_message_id, raw_message, formatted_message, "timestamp", is_recalled)
            VALUES (
                :user_id,
                :onebot_message_id,
                :raw_message,
                :formatted_message,
                :timestamp,
                :is_recalled
            )
            RETURNING id
        """)
        result = await self.session.execute(sql, params)
        saved_id = result.scalar()
        return int(saved_id) if saved_id is not None else 0

    async def get_message(
        self,
        group_id: int,
        local_message_id: int,
    ) -> dict | None:
        table_name = await self._get_table_name(group_id)

        sql = text(f"""
            SELECT * FROM {table_name} WHERE id = :local_message_id
        """)

        result = await self.session.execute(sql, {"local_message_id": local_message_id})
        row = result.first()

        if row:
            return dict(row._mapping)  # type: ignore

        return None

    async def get_message_by_onebot_message_id(
        self,
        group_id: int,
        onebot_message_id: str,
    ) -> dict | None:
        table_name = await self._get_table_name(group_id)

        sql = text(f"""
            SELECT * FROM {table_name}
            WHERE onebot_message_id = :onebot_message_id
            ORDER BY id DESC
            LIMIT 1
        """)

        result = await self.session.execute(
            sql,
            {"onebot_message_id": onebot_message_id},
        )
        row = result.first()
        if row:
            return dict(row._mapping)  # type: ignore

        return None

    async def mark_message_recalled_by_onebot_message_id(
        self,
        group_id: int,
        onebot_message_id: str,
    ) -> int:
        table_name = await self._get_table_name(group_id)

        sql = text(f"""
            UPDATE {table_name}
            SET is_recalled = true
            WHERE onebot_message_id = :onebot_message_id
              AND is_recalled = false
        """)

        result = await self.session.execute(
            sql,
            {"onebot_message_id": onebot_message_id},
        )
        return int(result.rowcount or 0)

    async def get_group_messages(
        self,
        group_id: int,
        limit: int = 100,
        offset: int = 0,
        include_recalled: bool = False,
    ) -> list[dict]:
        """Get messages from a group (paginated)."""
        table_name = await self._get_table_name(group_id)

        where_clause = ""
        if not include_recalled:
            where_clause = "WHERE is_recalled = false"

        sql = text(f"""
            SELECT * FROM {table_name}
            {where_clause}
            ORDER BY "timestamp" DESC
            LIMIT :limit OFFSET :offset
        """)

        result = await self.session.execute(sql, {"limit": limit, "offset": offset})
        rows = result.fetchall()

        return [dict(row._mapping) for row in rows]  # type: ignore

    async def get_recent_messages(
        self,
        group_id: int,
        limit: int = 10,
    ) -> list[dict]:
        """Get recent messages in chronological order (for context)."""
        table_name = await self._get_table_name(group_id)

        sql = text(f"""
            SELECT * FROM {table_name}
            WHERE is_recalled = false
            ORDER BY "timestamp" DESC
            LIMIT :limit
        """)

        result = await self.session.execute(sql, {"limit": limit})
        rows = result.fetchall()

        messages = [dict(row._mapping) for row in rows]  # type: ignore
        messages.reverse()

        return messages

    async def get_messages_before_timestamp(
        self,
        group_id: int,
        before_timestamp: datetime | int | float | None,
        limit: int = 10,
    ) -> list[dict]:
        table_name = await self._get_table_name(group_id)

        sql = text(f"""
            SELECT * FROM {table_name}
            WHERE is_recalled = false
              AND "timestamp" < :before_timestamp
            ORDER BY "timestamp" DESC
            LIMIT :limit
        """)

        result = await self.session.execute(
            sql,
            {
                "before_timestamp": normalize_china_time(before_timestamp),
                "limit": limit,
            },
        )
        rows = result.fetchall()

        messages = [dict(row._mapping) for row in rows]  # type: ignore
        messages.reverse()
        return messages

    async def get_user_messages_in_group(
        self,
        group_id: int,
        user_id: int,
        limit: int = 50,
    ) -> list[dict]:
        """Get recent messages from a specific user in a group."""
        table_name = await self._get_table_name(group_id)

        sql = text(f"""
            SELECT * FROM {table_name}
            WHERE user_id = :user_id AND is_recalled = false
            ORDER BY "timestamp" DESC
            LIMIT :limit
        """)

        result = await self.session.execute(sql, {"user_id": user_id, "limit": limit})
        rows = result.fetchall()

        return [dict(row._mapping) for row in rows]  # type: ignore

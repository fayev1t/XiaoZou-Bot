"""Group message management service."""

from sqlalchemy import (
    select,
    text,
)
from sqlalchemy.ext.asyncio import AsyncSession

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
        raw_message: str | None,
        formatted_message: str | None,
        is_recalled: bool = False,
    ) -> int:
        """Save a message to group message table."""
        table_name = await self._get_table_name(group_id)

        sql = text(f"""
            INSERT INTO {table_name}
            (user_id, raw_message, formatted_message, is_recalled)
            VALUES (
                :user_id,
                :raw_message,
                :formatted_message,
                :is_recalled
            )
            RETURNING id
        """)

        result = await self.session.execute(
            sql,
            {
                "user_id": user_id,
                "raw_message": raw_message,
                "formatted_message": formatted_message,
                "is_recalled": is_recalled,
            },
        )

        saved_id = result.scalar()
        return int(saved_id) if saved_id is not None else 0

    async def get_message(
        self,
        group_id: int,
        message_id: int,
    ) -> dict | None:
        """Get a specific message by auto id."""
        table_name = await self._get_table_name(group_id)

        sql = text(f"""
            SELECT * FROM {table_name} WHERE id = :message_id
        """)

        result = await self.session.execute(sql, {"message_id": message_id})
        row = result.first()

        if row:
            return dict(row._mapping)  # type: ignore

        return None

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

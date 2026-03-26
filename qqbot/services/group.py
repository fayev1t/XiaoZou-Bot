"""Group management service."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qqbot.core.database import create_group_tables, is_sqlite_backend
from qqbot.core.logging import get_logger
from qqbot.core.time import china_now
from qqbot.models import Group

logger = get_logger(__name__)
_verified_group_tables: set[int] = set()


class GroupService:
    """Service for managing groups."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_or_create_group(
        self,
        group_id: int,
        group_name: str | None = None,
    ) -> Group:
        result = await self.session.execute(
            select(Group).where(Group.group_id == group_id)
        )
        group = result.scalar_one_or_none()

        if group:
            if group_id not in _verified_group_tables:
                await create_group_tables(group_id)
                _verified_group_tables.add(group_id)
            if group_name and group.group_name != group_name:
                group.group_name = group_name
                group.updated_at = china_now()
            return group

        table_name = f"group_messages_v2_{group_id}"
        members_table_name = f"group_members_{group_id}"
        logger.info(f"[GroupService] Creating tables for group {group_id}...")
        try:
            await create_group_tables(group_id)
            _verified_group_tables.add(group_id)
            logger.info(f"[GroupService] ✅ Tables created for group {group_id}")
        except Exception as e:
            logger.error(
                "[GroupService] ❌ Failed to create tables for group %s: %s",
                group_id,
                e,
                exc_info=True,
            )
            raise

        if is_sqlite_backend():
            from sqlalchemy.dialects.sqlite import insert as dialect_insert
        else:
            from sqlalchemy.dialects.postgresql import insert as dialect_insert

        current_time = china_now()
        stmt = dialect_insert(Group).values(
            group_id=group_id,
            group_name=group_name,
            table_name=table_name,
            members_table_name=members_table_name,
            created_at=current_time,
            updated_at=current_time,
        ).on_conflict_do_nothing(index_elements=["group_id"])
        await self.session.execute(stmt)

        result = await self.session.execute(
            select(Group).where(Group.group_id == group_id)
        )
        group = result.scalar_one_or_none()
        if group is None:
            raise ValueError(f"Group {group_id} was not created")

        if group_name and group.group_name != group_name:
            group.group_name = group_name
            group.updated_at = china_now()

        return group

    async def get_group(
        self,
        group_id: int,
    ) -> Group | None:
        """Get a group by ID.

        Args:
            group_id: QQ group ID

        Returns:
            Group object or None if not found
        """
        result = await self.session.execute(
            select(Group).where(Group.group_id == group_id)
        )
        return result.scalar_one_or_none()

    async def get_all_groups(
        self,
    ) -> list[Group]:
        """Get all groups from database.

        Returns:
            List of Group objects
        """
        result = await self.session.execute(select(Group))
        return result.scalars().all()

    async def update_group_name(
        self,
        group_id: int,
        group_name: str,
    ) -> Group:
        result = await self.session.execute(
            select(Group).where(Group.group_id == group_id)
        )
        group = result.scalar_one_or_none()

        if not group:
            raise ValueError(f"Group {group_id} not found")

        group.group_name = group_name
        group.updated_at = china_now()

        return group

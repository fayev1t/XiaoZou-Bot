"""Group management service."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qqbot.models import Group
from qqbot.core.database import create_group_tables, table_exists
from qqbot.core.logging import get_logger

logger = get_logger(__name__)


class GroupService:
    """Service for managing groups."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_or_create_group(
        self,
        group_id: int,
        group_name: str | None = None,
    ) -> Group:
        """Get an existing group or create a new one.

        Args:
            group_id: QQ group ID
            group_name: Group name (optional)

        Returns:
            Group object
        """
        # Try to get existing group
        result = await self.session.execute(
            select(Group).where(Group.group_id == group_id)
        )
        group = result.scalar_one_or_none()

        if group:
            # Verify tables exist for existing group
            # (in case table creation failed previously)
            members_exists = await table_exists(group.members_table_name)
            messages_exists = await table_exists(group.table_name)

            if not members_exists or not messages_exists:
                logger.warning(
                    (
                        "[GroupService] Tables missing for existing group %s, "
                        "recreating..."
                    ),
                    group_id,
                )
                try:
                    await create_group_tables(group_id)
                    logger.info(
                        "[GroupService] ✅ Tables recreated for group %s",
                        group_id,
                    )
                except Exception as e:
                    logger.error(
                        (
                            "[GroupService] ❌ Failed to recreate tables for %s: %s"
                        ),
                        group_id,
                        e,
                        exc_info=True,
                    )
                    raise

            # Update name if provided and different
            if group_name and group.group_name != group_name:
                group.group_name = group_name
                await self.session.commit()
                await self.session.refresh(group)
            return group

        # Create new group
        table_name = f"group_messages_v2_{group_id}"
        members_table_name = f"group_members_{group_id}"

        group = Group(
            group_id=group_id,
            group_name=group_name,
            table_name=table_name,
            members_table_name=members_table_name,
        )

        # Create the per-group tables FIRST (before committing Group record)
        # Tables must exist before any data can be inserted
        logger.info(f"[GroupService] Creating tables for group {group_id}...")
        try:
            await create_group_tables(group_id)
            logger.info(f"[GroupService] ✅ Tables created for group {group_id}")
        except Exception as e:
            logger.error(
                "[GroupService] ❌ Failed to create tables for group %s: %s",
                group_id,
                e,
                exc_info=True,
            )
            raise

        # Only commit Group record after tables are successfully created
        self.session.add(group)
        await self.session.commit()
        await self.session.refresh(group)
        logger.info(f"[GroupService] ✅ Group record saved for {group_id}")

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
        """Update group name.

        Args:
            group_id: QQ group ID
            group_name: New group name

        Returns:
            Updated Group object
        """
        result = await self.session.execute(
            select(Group).where(Group.group_id == group_id)
        )
        group = result.scalar_one_or_none()

        if not group:
            raise ValueError(f"Group {group_id} not found")

        group.group_name = group_name
        await self.session.commit()
        await self.session.refresh(group)

        return group

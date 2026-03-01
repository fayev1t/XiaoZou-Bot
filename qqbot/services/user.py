"""User service for user data management."""

from datetime import datetime
from sqlalchemy import select, insert, update
from sqlalchemy.ext.asyncio import AsyncSession

from qqbot.models import User


class UserService:
    """Service for managing user data."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_or_create_user(
        self,
        user_id: int,
        nickname: str | None = None,
    ) -> User:
        """Get existing user or create new one (idempotent).

        Args:
            user_id: QQ user ID
            nickname: User nickname (optional)

        Returns:
            User object
        """
        # Try to get existing user
        result = await self.session.execute(
            select(User).where(User.user_id == user_id)
        )
        user = result.scalar_one_or_none()

        if user:
            return user

        # Create new user
        new_user = User(
            user_id=user_id,
            nickname=nickname,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        self.session.add(new_user)
        await self.session.flush()  # Flush to get the id
        return new_user

    async def get_user(
        self,
        user_id: int,
    ) -> User | None:
        """Get user by user_id.

        Args:
            user_id: QQ user ID

        Returns:
            User object or None if not found
        """
        result = await self.session.execute(
            select(User).where(User.user_id == user_id)
        )
        return result.scalar_one_or_none()

    async def update_user_nickname(
        self,
        user_id: int,
        nickname: str,
    ) -> None:
        """Update user nickname.

        Args:
            user_id: QQ user ID
            nickname: New nickname
        """
        stmt = (
            update(User)
            .where(User.user_id == user_id)
            .values(
                nickname=nickname,
                updated_at=datetime.utcnow(),
            )
        )
        await self.session.execute(stmt)

    async def batch_update_nicknames(
        self,
        user_updates: dict[int, str],
    ) -> None:
        """Batch update user nicknames (for background sync task).

        Uses UPSERT to insert new users or update existing ones.

        Args:
            user_updates: Dict of {user_id: new_nickname}
        """
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        for user_id, nickname in user_updates.items():
            if nickname:  # Only update if nickname is not empty
                # Use PostgreSQL UPSERT (INSERT ... ON CONFLICT DO UPDATE)
                stmt = pg_insert(User).values(
                    user_id=user_id,
                    nickname=nickname,
                    created_at=datetime.utcnow(),
                    updated_at=datetime.utcnow(),
                ).on_conflict_do_update(
                    index_elements=["user_id"],
                    set_={
                        "nickname": nickname,
                        "updated_at": datetime.utcnow(),
                    }
                )
                await self.session.execute(stmt)

    async def get_user_by_id(
        self,
        user_id: int,
    ) -> dict | None:
        """Get user data as dict.

        Args:
            user_id: QQ user ID

        Returns:
            User data dict or None if not found
        """
        user = await self.get_user(user_id)
        if user:
            return {
                "id": user.id,
                "user_id": user.user_id,
                "nickname": user.nickname,
                "created_at": user.created_at,
                "updated_at": user.updated_at,
            }
        return None

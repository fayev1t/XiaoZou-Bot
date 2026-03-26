"""User service for user data management."""
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from qqbot.core.database import is_sqlite_backend
from qqbot.core.time import china_now
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
        result = await self.session.execute(
            select(User).where(User.user_id == user_id)
        )
        user = result.scalar_one_or_none()

        if user:
            if nickname and user.nickname != nickname:
                user.nickname = nickname
                user.updated_at = china_now()
            return user

        if is_sqlite_backend():
            from sqlalchemy.dialects.sqlite import insert as dialect_insert
        else:
            from sqlalchemy.dialects.postgresql import insert as dialect_insert

        current_time = china_now()
        stmt = dialect_insert(User).values(
            user_id=user_id,
            nickname=nickname,
            created_at=current_time,
            updated_at=current_time,
        ).on_conflict_do_nothing(index_elements=["user_id"])
        await self.session.execute(stmt)

        result = await self.session.execute(
            select(User).where(User.user_id == user_id)
        )
        user = result.scalar_one_or_none()
        if user is None:
            raise ValueError(f"User {user_id} was not created")

        if nickname and user.nickname != nickname:
            user.nickname = nickname
            user.updated_at = china_now()

        return user

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
                updated_at=china_now(),
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
        if is_sqlite_backend():
            from sqlalchemy.dialects.sqlite import insert as dialect_insert
        else:
            from sqlalchemy.dialects.postgresql import insert as dialect_insert

        for user_id, nickname in user_updates.items():
            if nickname:  # Only update if nickname is not empty
                current_time = china_now()
                stmt = dialect_insert(User).values(
                    user_id=user_id,
                    nickname=nickname,
                    created_at=current_time,
                    updated_at=current_time,
                ).on_conflict_do_update(
                    index_elements=["user_id"],
                    set_={
                        "nickname": nickname,
                        "updated_at": current_time,
                    }
                )
                await self.session.execute(stmt)

    async def get_user_by_id(
        self,
        user_id: int,
    ) -> dict[str, object] | None:
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

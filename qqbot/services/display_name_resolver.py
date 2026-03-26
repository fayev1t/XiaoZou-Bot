"""Display name resolver for group members."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from qqbot.core.logging import get_logger
from qqbot.services.group_member import GroupMemberService
from qqbot.services.user import UserService

logger = get_logger(__name__)


class DisplayNameResolver:
    """Resolve display names for users in a group."""

    @staticmethod
    async def resolve(
        session: AsyncSession,
        group_id: int,
        user_id: int | str | None,
        name_cache: dict[int, str] | None = None,
    ) -> str:
        if user_id is None:
            return "无"

        if isinstance(user_id, str) and user_id.lower() == "all":
            return "全体成员"

        try:
            user_id_int = int(user_id)
        except (TypeError, ValueError):
            return "无"

        if name_cache and user_id_int in name_cache:
            return name_cache[user_id_int]

        display_name = f"用户{user_id_int}"

        try:
            member_service = GroupMemberService(session)
            user_service = UserService(session)
            member = await member_service.get_member(group_id, user_id_int)
            if member and member.get("card"):
                display_name = member["card"]
            else:
                user = await user_service.get_user(user_id_int)
                if user and user.nickname:
                    display_name = user.nickname
        except Exception as exc:
            logger.debug(
                "[display_name_resolver] resolve failed",
                extra={
                    "group_id": group_id,
                    "user_id": user_id_int,
                    "error": str(exc),
                },
            )

        if name_cache is not None:
            name_cache[user_id_int] = display_name

        return display_name

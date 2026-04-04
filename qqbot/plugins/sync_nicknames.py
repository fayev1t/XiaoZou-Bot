"""Background task to sync group member nicknames."""

import asyncio
from nonebot.adapters.onebot.v11 import Bot

from qqbot.core.database import AsyncSessionLocal
from qqbot.core.logging import get_logger
from qqbot.services.group import GroupService
from qqbot.services.user import UserService
from qqbot.services.group_member import GroupMemberService

logger = get_logger(__name__)


async def sync_all_group_nicknames(bot: Bot) -> None:
    logger.info("🔄 *** sync_all_group_nicknames STARTED ***")
    try:
        async with AsyncSessionLocal() as session:
            group_service = GroupService(session)
            all_groups = [
                {"group_id": group.group_id, "group_name": group.group_name}
                for group in await group_service.get_all_groups()
            ]
            logger.info(f"📊 Found {len(all_groups) if all_groups else 0} groups in database")

            if not all_groups:
                logger.warning("⚠️ No groups found in database - skipping sync")
                return

            logger.info(f"🔄 Starting sync for {len(all_groups)} groups...")

            for group in all_groups:
                group_id = group["group_id"]
                group_name = group["group_name"]
                try:
                    new_group_name: str | None = None
                    try:
                        group_info = await asyncio.wait_for(
                            bot.get_group_info(group_id=group_id),
                            timeout=5.0
                        )
                        new_group_name = group_info.get("group_name")
                    except asyncio.TimeoutError:
                        logger.debug(f"[sync_nicknames] ⏱️ Timeout fetching group info for {group_id}")
                    except Exception as e:
                        logger.debug(f"[sync_nicknames] Failed to update group name for {group_id}: {e}")

                    try:
                        members_list = await asyncio.wait_for(
                            bot.get_group_member_list(group_id=group_id),
                            timeout=10.0
                        )
                    except asyncio.TimeoutError:
                        logger.warning(f"[sync_nicknames] ⏱️ Timeout fetching member list for group {group_id}")
                        continue
                    except Exception as e:
                        logger.warning(f"[sync_nicknames] Failed to fetch member list for group {group_id}: {e}")
                        continue

                    if not members_list:
                        logger.warning(f"[sync_nicknames] ⚠️ No members in group {group_id}")
                        continue

                    logger.info(f"[sync_nicknames] 📥 Got {len(members_list)} members from group {group_id}")

                    nickname_updates: dict[int, str] = {}
                    card_updates: dict[int, str] = {}

                    for member_info in members_list:
                        user_id = member_info.get("user_id")
                        nickname = member_info.get("nickname")
                        card = member_info.get("card")

                        if user_id and nickname:
                            nickname_updates[user_id] = nickname

                        if user_id and card:
                            card_updates[user_id] = card

                    async with AsyncSessionLocal() as session:
                        group_service = GroupService(session)
                        user_service = UserService(session)
                        member_service = GroupMemberService(session)

                        if new_group_name and new_group_name != group_name:
                            await group_service.update_group_name(
                                group_id,
                                new_group_name,
                            )
                            logger.info(
                                "[sync_nicknames] 📝 Group name updated: {} → {}",
                                group_name,
                                new_group_name,
                                extra={"group_id": group_id},
                            )

                        if nickname_updates:
                            await user_service.batch_update_nicknames(nickname_updates)
                            logger.info(f"👤 Updated {len(nickname_updates)} user QQ nicknames")
                        else:
                            logger.debug("👤 No nickname updates")

                        if card_updates:
                            await member_service.batch_update_cards(
                                group_id=group_id,
                                card_updates=card_updates,
                            )
                            logger.info(f"🏷️ Updated {len(card_updates)} group member cards")
                        else:
                            logger.debug("🏷️ No card updates")

                        await session.commit()

                    logger.info(
                        f"[sync_nicknames] ✅ Group {group_id}: 1 group name + {len(members_list)} members synced",
                        extra={"group_id": group_id},
                    )

                except Exception as e:
                    logger.error(
                        f"[sync_nicknames] Failed to sync group {group_id}: {e}",
                        exc_info=True,
                    )
                    continue

            logger.info("✨ All groups synced successfully!")

    except Exception as e:
        logger.exception("❌ Fatal error: {}", e)

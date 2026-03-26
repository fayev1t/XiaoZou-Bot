"""Event handlers for database operations.

This plugin listens to QQ Bot events and performs database operations:
- GroupMessageEvent: Save messages to database
- GroupIncreaseNoticeEvent: Add member to database
- GroupDecreaseNoticeEvent: Mark member as inactive
- GroupRecallNoticeEvent: Mark message as recalled
- Group name & member nickname sync: Every 30 minutes (background task in sync_nicknames.py)
"""

from nonebot import on_notice, on_message
from nonebot.adapters import Event
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    GroupIncreaseNoticeEvent,
    GroupDecreaseNoticeEvent,
    GroupRecallNoticeEvent,
)
from nonebot.rule import Rule

from qqbot.core.database import AsyncSessionLocal
from qqbot.core.logging import get_logger
from qqbot.core.settings import get_bot_nicknames
from qqbot.services.user import UserService
from qqbot.services.group import GroupService
from qqbot.services.group_message import GroupMessageService
from qqbot.services.group_member import GroupMemberService
from qqbot.services.message_aggregator import message_aggregator
from qqbot.services.message_pipeline import MessagePipeline

logger = get_logger(__name__)
BOT_NICKNAMES = get_bot_nicknames()


def _is_group_message(event: Event) -> bool:
    return isinstance(event, GroupMessageEvent)


def _is_group_increase_notice(event: Event) -> bool:
    return isinstance(event, GroupIncreaseNoticeEvent)


def _is_group_decrease_notice(event: Event) -> bool:
    return isinstance(event, GroupDecreaseNoticeEvent)


def _is_group_recall_notice(event: Event) -> bool:
    return isinstance(event, GroupRecallNoticeEvent)

# ============================================================================
# 1. GroupMessageEvent - 保存群消息到数据库
# ============================================================================

message_handler = on_message(rule=Rule(_is_group_message), priority=10, block=False)
message_pipeline = MessagePipeline()


@message_handler.handle()
async def handle_group_message(bot: Bot, event: GroupMessageEvent) -> None:
    _ = bot

    group_id = event.group_id
    user_id = event.user_id
    track_persist = user_id != event.self_id

    if track_persist:
        await message_aggregator.begin_message_persist(group_id)

    async with AsyncSessionLocal() as session:
        try:
            record, saved_id = await message_pipeline.process_event(session, event)

            # Commit all operations together so group_chat handler sees the message.
            await session.commit()

            logger.info(
                "Message saved successfully",
                extra={
                    "group_id": group_id,
                    "user_id": user_id,
                    "message_id": saved_id,
                    "onebot_message_id": getattr(event, "message_id", None),
                    "content_length": len(record.formatted_message),
                    "message_type": record.message_type,
                },
            )

            try:
                if user_id == event.self_id:
                    await message_aggregator.complete_message_persist(group_id)
                    return

                message_source = getattr(event, "original_message", None) or event.message
                is_bot_mentioned = getattr(event, "to_me", False)
                if not is_bot_mentioned:
                    is_bot_mentioned = any(
                        segment.type == "at"
                        and str(segment.data.get("qq")) == str(event.self_id)
                        for segment in message_source
                    )
                if not is_bot_mentioned:
                    is_bot_mentioned = any(
                        nickname in record.raw_message
                        for nickname in BOT_NICKNAMES
                    )

                if not record.raw_message.strip() and not is_bot_mentioned:
                    await message_aggregator.complete_message_persist(group_id)
                    return

                await message_aggregator.finish_message_persist_and_add_message(
                    group_id=group_id,
                    user_id=user_id,
                    formatted_message=record.formatted_message,
                    event=event,
                    is_bot_mentioned=is_bot_mentioned,
                )
            except Exception as exc:
                if track_persist:
                    await message_aggregator.fail_message_persist(group_id)
                logger.error(
                    "Failed to add message to aggregator: %s",
                    exc,
                    extra={"group_id": group_id, "user_id": user_id},
                )

        except ValueError as e:
            await session.rollback()
            if track_persist:
                await message_aggregator.fail_message_persist(group_id)
            logger.warning(f"Message event error: {e}", extra={"group_id": group_id})
        except Exception as e:
            await session.rollback()
            if track_persist:
                await message_aggregator.fail_message_persist(group_id)
            logger.error(
                f"Failed to save message: {e}",
                extra={"group_id": group_id, "user_id": user_id},
            )


# ============================================================================
# 2. GroupIncreaseNoticeEvent - 成员进群
# ============================================================================

increase_handler = on_notice(
    rule=Rule(_is_group_increase_notice),
    priority=5,
    block=False,
)


@increase_handler.handle()
async def handle_group_increase(bot: Bot, event: GroupIncreaseNoticeEvent) -> None:
    _ = bot
    group_id = event.group_id
    user_id = event.user_id

    async with AsyncSessionLocal() as session:
        try:
            user_service = UserService(session)
            group_service = GroupService(session)
            member_service = GroupMemberService(session)
            await user_service.get_or_create_user(user_id=user_id)
            await group_service.get_or_create_group(group_id=group_id)
            await member_service.add_member_from_join_event(
                group_id=group_id,
                user_id=user_id,
            )
            await session.commit()

            logger.info(
                "Member added to group",
                extra={
                    "group_id": group_id,
                    "user_id": user_id,
                    "operator_id": event.operator_id,
                },
            )

        except ValueError as e:
            await session.rollback()
            logger.warning(f"Join event error: {e}", extra={"group_id": group_id})
        except Exception as e:
            await session.rollback()
            logger.error(
                f"Failed to add member: {e}",
                extra={"group_id": group_id, "user_id": user_id},
            )


# ============================================================================
# 3. GroupDecreaseNoticeEvent - 成员离群
# ============================================================================

decrease_handler = on_notice(
    rule=Rule(_is_group_decrease_notice),
    priority=5,
    block=False,
)


@decrease_handler.handle()
async def handle_group_decrease(bot: Bot, event: GroupDecreaseNoticeEvent) -> None:
    _ = bot
    group_id = event.group_id
    user_id = event.user_id

    async with AsyncSessionLocal() as session:
        try:
            member_service = GroupMemberService(session)
            # 标记成员为离线（软删除）
            await member_service.mark_member_inactive(
                group_id=group_id,
                user_id=user_id,
            )
            await session.commit()

            logger.info(
                "Member left group",
                extra={
                    "group_id": group_id,
                    "user_id": user_id,
                    "operator_id": event.operator_id,
                },
            )

        except ValueError as e:
            await session.rollback()
            logger.warning(f"Leave event error: {e}", extra={"group_id": group_id})
        except Exception as e:
            await session.rollback()
            logger.error(
                f"Failed to mark member inactive: {e}",
                extra={"group_id": group_id, "user_id": user_id},
            )


# ============================================================================
# 4. GroupRecallNoticeEvent - 消息撤回
# ============================================================================

recall_handler = on_notice(
    rule=Rule(_is_group_recall_notice),
    priority=10,
    block=False,
)


@recall_handler.handle()
async def handle_group_recall(bot: Bot, event: GroupRecallNoticeEvent) -> None:
    _ = bot
    group_id = event.group_id
    raw_message_id = getattr(event, "message_id", None)
    onebot_message_id = str(raw_message_id).strip() if raw_message_id is not None else ""

    if not onebot_message_id:
        logger.warning(
            "Recall event missing message id",
            extra={"group_id": group_id, "operator_id": event.operator_id},
        )
        return

    async with AsyncSessionLocal() as session:
        try:
            group_service = GroupService(session)
            await group_service.get_or_create_group(group_id=group_id)

            message_service = GroupMessageService(session)
            updated_rows = await message_service.mark_message_recalled_by_onebot_message_id(
                group_id=group_id,
                onebot_message_id=onebot_message_id,
            )
            await session.commit()

            logger.info(
                "Recall event processed",
                extra={
                    "group_id": group_id,
                    "message_id": onebot_message_id,
                    "operator_id": event.operator_id,
                    "updated_rows": updated_rows,
                },
            )
        except Exception as e:
            await session.rollback()
            logger.error(
                f"Failed to mark recalled message: {e}",
                extra={"group_id": group_id, "message_id": onebot_message_id},
            )

"""Event handlers for database operations.

This plugin listens to QQ Bot events and performs database operations:
- GroupMessageEvent: Save messages to database
- GroupIncreaseNoticeEvent: Add member to database
- GroupDecreaseNoticeEvent: Mark member as inactive
- GroupRecallNoticeEvent: Mark message as recalled
- Group name & member nickname sync: Every 30 minutes (background task in sync_nicknames.py)
"""

from nonebot import on_notice, on_message
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    GroupIncreaseNoticeEvent,
    GroupDecreaseNoticeEvent,
    GroupRecallNoticeEvent,
)

from qqbot.core.database import AsyncSessionLocal
from qqbot.core.logging import get_logger, log_event
from qqbot.services.user import UserService
from qqbot.services.group import GroupService
from qqbot.services.group_member import GroupMemberService
from qqbot.services.message_aggregator import message_aggregator
from qqbot.services.message_pipeline import MessagePipeline

logger = get_logger(__name__)

# ============================================================================
# 1. GroupMessageEvent - 保存群消息到数据库
# ============================================================================

message_handler = on_message(priority=10, block=False)
message_pipeline = MessagePipeline()


@message_handler.handle()
async def handle_group_message(bot: Bot, event: GroupMessageEvent) -> None:
    """Handle group message event - save to database.

    Priority: P0 - 高频操作，同步执行
    """
    # Only handle group messages, not private messages
    if not hasattr(event, "group_id"):
        return

    group_id = event.group_id
    user_id = event.user_id

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
                    return

                message_source = getattr(event, "original_message", None) or event.message
                is_bot_mentioned = getattr(event, "to_me", False)
                if not is_bot_mentioned:
                    is_bot_mentioned = any(
                        segment.type == "at"
                        and str(segment.data.get("qq")) == str(event.self_id)
                        for segment in message_source
                    )
                if not is_bot_mentioned and "小奏" in record.raw_message:
                    is_bot_mentioned = True

                if not record.raw_message.strip() and not is_bot_mentioned:
                    return

                await message_aggregator.add_message(
                    group_id=group_id,
                    user_id=user_id,
                    raw_message=record.raw_message,
                    formatted_message=record.formatted_message,
                    event=event,
                    is_bot_mentioned=is_bot_mentioned,
                )
            except Exception as exc:
                logger.error(
                    "Failed to add message to aggregator: %s",
                    exc,
                    extra={"group_id": group_id, "user_id": user_id},
                )

        except ValueError as e:
            logger.warning(f"Message event error: {e}", extra={"group_id": group_id})
        except Exception as e:
            logger.error(
                f"Failed to save message: {e}",
                extra={"group_id": group_id, "user_id": user_id},
            )


# ============================================================================
# 2. GroupIncreaseNoticeEvent - 成员进群
# ============================================================================

increase_handler = on_notice(priority=5, block=False)


@increase_handler.handle()
async def handle_group_increase(bot: Bot, event: GroupIncreaseNoticeEvent) -> None:
    """Handle group member join event.

    Priority: P0 - 必需操作，同步执行
    Idempotency: ✅ ON CONFLICT处理重复事件
    """
    group_id = event.group_id
    user_id = event.user_id

    async with AsyncSessionLocal() as session:
        try:
            user_service = UserService(session)
            group_service = GroupService(session)
            member_service = GroupMemberService(session)
            # 1. 确保用户存在
            await user_service.get_or_create_user(user_id=user_id)
            await session.commit()

            # 2. 确保群存在
            await group_service.get_or_create_group(group_id=group_id)
            await session.commit()

            # 3. 添加成员（幂等操作）
            await member_service.add_member_from_join_event(
                group_id=group_id,
                user_id=user_id,
            )
            await session.commit()

            # Note: 昵称更新现在由后台任务定期处理，避免频繁调用 QQ API

            logger.info(
                "Member added to group",
                extra={
                    "group_id": group_id,
                    "user_id": user_id,
                    "operator_id": event.operator_id,
                },
            )

        except ValueError as e:
            logger.warning(f"Join event error: {e}", extra={"group_id": group_id})
        except Exception as e:
            logger.error(
                f"Failed to add member: {e}",
                extra={"group_id": group_id, "user_id": user_id},
            )


# ============================================================================
# 3. GroupDecreaseNoticeEvent - 成员离群
# ============================================================================

decrease_handler = on_notice(priority=5, block=False)


@decrease_handler.handle()
async def handle_group_decrease(bot: Bot, event: GroupDecreaseNoticeEvent) -> None:
    """Handle group member leave event.

    Priority: P0 - 必需操作，同步执行
    Idempotency: ✅ UPDATE无唯一性约束，多次执行安全
    """
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
            logger.warning(f"Leave event error: {e}", extra={"group_id": group_id})
        except Exception as e:
            logger.error(
                f"Failed to mark member inactive: {e}",
                extra={"group_id": group_id, "user_id": user_id},
            )


# ============================================================================
# 4. GroupRecallNoticeEvent - 消息撤回
# ============================================================================

recall_handler = on_notice(priority=10, block=False)


@recall_handler.handle()
async def handle_group_recall(bot: Bot, event: GroupRecallNoticeEvent) -> None:
    """Handle group message recall event.

    Priority: P1 - 异步优先级，非关键操作
    Idempotency: ✅ no-op
    """
    group_id = event.group_id
    logger.info(
        "Recall event received (message_id column removed, skipping persist)",
        extra={
            "group_id": group_id,
            "message_id": getattr(event, "message_id", None),
            "operator_id": event.operator_id,
        },
    )

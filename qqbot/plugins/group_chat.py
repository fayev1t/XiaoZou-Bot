"""Group chat AI conversation plugin with message aggregation.

This plugin enables the bot to participate naturally in group conversations
using a message aggregation system and two-tier AI:
1. Message Aggregator: Collects messages into response blocks
2. Block Judger (first-tier): Analyzes block and decides reply strategy
3. Conversation Service (second-tier): Generates appropriate responses

Execution order (lower priority runs first):
- Priority 10: event_handlers.py (saves message and feeds aggregator)
- Priority 50: group_chat.py (binds bot instance; handles responses)

The aggregation mechanism:
- Messages are collected into a "response block" per group
- After receiving a message, ask the wait-time judge how long to wait
- If new messages arrive, reset the timer and add to block
- When the wait expires, analyze the entire block and respond
"""

import asyncio

from nonebot import on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent

from qqbot.core.database import AsyncSessionLocal
from qqbot.core.logging import get_logger, log_event
from qqbot.services.context import ContextManager
from qqbot.services.conversation import ConversationService
from qqbot.services.group_message import GroupMessageService
from qqbot.services.group_member import GroupMemberService
from qqbot.services.user import UserService
from qqbot.services.message_converter import MessageConverter
from qqbot.services.message_aggregator import ResponseBlock, message_aggregator
from qqbot.services.block_judge import block_judger, JudgeResult

logger = get_logger(__name__)

# Handler with priority 50 (after message saving at priority 10)
group_chat_handler = on_message(priority=50, block=False)

# Store bot instance for callback use
_bot_instance: Bot | None = None
message_converter = MessageConverter()


async def _process_response_block(group_id: int, block: ResponseBlock) -> None:
    """Process a response block and generate replies.

    This is the callback function called by the aggregator when
    a block is ready to be processed.

    Args:
        group_id: QQ group ID
        block: The response block containing aggregated messages
    """
    global _bot_instance

    if not _bot_instance:
        logger.error("[group_chat] No bot instance available")
        return

    if block.get_message_count() == 0:
        logger.debug(f"[group_chat] Empty block for group {group_id}, skipping")
        return

    bot = _bot_instance

    logger.info(
        f"[group_chat] ══════ 开始处理对话块 ══════ 群={group_id}, "
        f"消息数={block.get_message_count()}, 用户数={len(block.get_unique_users())}",
        extra={
            "group_id": group_id,
            "message_count": block.get_message_count(),
            "unique_users": len(block.get_unique_users()),
        },
    )
    print(f"[group_chat] ══════ 开始处理对话块 ══════ 群={group_id}, 消息数={block.get_message_count()}, 用户数={len(block.get_unique_users())}")

    try:
        async with AsyncSessionLocal() as session:
            # 1. Get user names for the block
            user_names: dict[int, str] = {}
            member_service = GroupMemberService(session)
            user_service = UserService(session)
            for user_id in block.get_unique_users():
                try:
                    # Try group card first
                    member = await member_service.get_member(group_id, user_id)
                    if member and member.get("card"):
                        user_names[user_id] = member["card"]
                    else:
                        # Fallback to user nickname
                        user = await user_service.get_user(user_id)
                        if user and user.get("nickname"):
                            user_names[user_id] = user["nickname"]
                        else:
                            user_names[user_id] = f"用户{user_id}"
                except Exception:
                    user_names[user_id] = f"用户{user_id}"

            # 2. Get historical context from database
            context_manager = ContextManager(session)
            context = await context_manager.get_recent_context(
                group_id=group_id,
                limit=50,  # Layer 1: 使用50条历史消息提供深层话题背景
                bot_id=block.messages[0].event.self_id if block.messages else None,
            )

            # 3. Judge the block (first-tier AI)
            msg = f"[group_chat] 🧠 第一层AI判断块内容..."
            logger.info(msg, extra={"group_id": group_id})
            print(msg)

            judge_result = await block_judger.judge_block(
                block=block,
                context=context,
                group_id=group_id,
                user_names=user_names,
            )

            # Early exit if no reply needed
            if not judge_result.should_reply or judge_result.reply_count == 0:
                msg = f"[group_chat] ❌ 不需要回复 | 群={group_id}, 原因={judge_result.explanation}"
                logger.info(msg, extra={"group_id": group_id})
                print(msg)
                return

            # 4. Generate and send responses for each reply plan
            conversation = ConversationService()

            msg = f"[group_chat] ✅ 准备生成回复 | 群={group_id}, 需要{len(judge_result.replies)}条回复"
            logger.info(msg, extra={"group_id": group_id, "reply_count": len(judge_result.replies)})
            print(msg)

            for i, reply_plan in enumerate(judge_result.replies):
                msg = f"[group_chat] 🔷 正在生成第 {i + 1}/{len(judge_result.replies)} 条回复 | 态度={reply_plan.emotion}, @用户={reply_plan.target_user_id}"
                logger.info(msg, extra={
                    "group_id": group_id,
                    "reply_index": i + 1,
                    "emotion": reply_plan.emotion,
                    "target_user_id": reply_plan.target_user_id,
                })
                print(msg)

                # Convert ReplyPlan to JudgeResult for ConversationService
                # This maintains compatibility with existing conversation service
                legacy_judge_result = JudgeResult(
                    should_reply=True,
                    reply_type="general",  # Default to general as types are removed
                    target_user_id=reply_plan.target_user_id,
                    emotion=reply_plan.emotion,
                    instruction=reply_plan.instruction,
                    should_mention=reply_plan.should_mention,
                )

                # Build context with block content
                block_context = f"{context}\n\n【当前对话块摘要】\n{judge_result.block_summary}\n\n【相关消息】\n{reply_plan.related_messages}"

                # Generate response (second-tier AI)
                response = await conversation.generate_response(
                    session=session,
                    context=block_context,
                    judge_result=legacy_judge_result,
                    group_id=group_id,
                )

                # Send response
                send_result = await bot.send_group_msg(
                    group_id=group_id,
                    message=response,
                )

                msg = f"[group_chat] 📤 第 {i + 1} 条回复已发送 | 群={group_id}, 长度={len(response)}, 内容={response[:50]}"
                logger.info(msg, extra={
                    "group_id": group_id,
                    "reply_index": i + 1,
                    "response_length": len(response),
                })
                print(msg)

                # Save bot's response to database
                try:
                    # Get bot's self_id from first message in block
                    bot_self_id = (
                        block.messages[0].event.self_id if block.messages else None
                    )
                    if bot_self_id:
                        message_service = GroupMessageService(session)
                        formatted = message_converter.wrap_plain_text(
                            response,
                            user_id=bot_self_id,
                            display_name="小奏",
                        )
                        await message_service.save_message(
                            group_id=group_id,
                            user_id=bot_self_id,
                            raw_message=response,
                            formatted_message=formatted,
                        )
                        await session.commit()
                except Exception as e:
                    logger.warning(
                        f"[group_chat] Failed to save bot response: {e}",
                        extra={"group_id": group_id},
                    )

                # If there are more replies, wait a bit before sending next
                if i < len(judge_result.replies) - 1:
                    msg = f"[group_chat] ⏳ 等待1秒后发送下一条回复 | 群={group_id}"
                    logger.debug(msg, extra={"group_id": group_id})
                    print(msg)
                    await asyncio.sleep(1.0)  # 1 second between multiple replies

            msg = f"[group_chat] 🎉 对话块处理完成 | 群={group_id}, 共发送{len(judge_result.replies)}条回复"
            logger.info(msg, extra={
                "group_id": group_id,
                "total_replies": len(judge_result.replies),
            })
            print(msg)

    except Exception as e:
        logger.error(
            f"[group_chat] Failed to process block for group {group_id}: {e}",
            exc_info=True,
        )


# Register the callback with the aggregator
message_aggregator.set_reply_callback(_process_response_block)


@group_chat_handler.handle()
async def handle_group_chat(bot: Bot, event: GroupMessageEvent) -> None:
    """Bind bot instance for later reply callback."""
    global _bot_instance
    _bot_instance = bot  # Store for callback use
    return

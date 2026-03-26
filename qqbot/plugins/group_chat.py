"""Group chat AI conversation plugin with message aggregation."""

import asyncio

from nonebot import on_message
from nonebot.adapters import Event
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent
from nonebot.rule import Rule

from qqbot.core.database import AsyncSessionLocal
from qqbot.core.logging import get_logger
from qqbot.core.settings import get_primary_bot_name
from qqbot.core.time import china_now
from qqbot.services.context import ContextManager
from qqbot.services.conversation import conversation_service
from qqbot.services.image_parsing import ImageParsingService
from qqbot.services.group_message import GroupMessageService
from qqbot.services.group_member import GroupMemberService
from qqbot.services.user import UserService
from qqbot.services.message_converter import MessageConverter
from qqbot.services.message_aggregator import ResponseBlock, message_aggregator
from qqbot.services.block_judge import block_judger, JudgeResult

logger = get_logger(__name__)


def _is_group_message(event: Event) -> bool:
    return isinstance(event, GroupMessageEvent)

group_chat_handler = on_message(rule=Rule(_is_group_message), priority=50, block=False)

_bot_instances: dict[str, Bot] = {}
message_converter = MessageConverter()
BOT_DISPLAY_NAME = get_primary_bot_name()


def _remember_bot(bot: Bot) -> None:
    _bot_instances[str(bot.self_id)] = bot


def _extract_sent_message_id(send_result: object) -> str | None:
    if send_result is None:
        return None

    if isinstance(send_result, dict):
        message_id = send_result.get("message_id")
    else:
        message_id = getattr(send_result, "message_id", send_result)

    if message_id is None:
        return None

    normalized = str(message_id).strip()
    return normalized or None


def _get_bot_for_block(block: ResponseBlock) -> Bot | None:
    if not block.messages:
        return None

    bot_self_id = getattr(block.messages[0].event, "self_id", None)
    if bot_self_id is not None:
        bot = _bot_instances.get(str(bot_self_id))
        if bot is not None:
            return bot

    return next(iter(_bot_instances.values()), None)


async def _process_response_block(group_id: int, block: ResponseBlock) -> None:
    bot = _get_bot_for_block(block)
    if bot is None:
        logger.error("[group_chat] No bot instance available")
        return

    if block.get_message_count() == 0:
        logger.debug(f"[group_chat] Empty block for group {group_id}, skipping")
        return

    logger.info(
        f"[group_chat] ══════ 开始处理对话块 ══════ 群={group_id}, "
        f"消息数={block.get_message_count()}, 用户数={len(block.get_unique_users())}",
        extra={
            "group_id": group_id,
            "message_count": block.get_message_count(),
            "unique_users": len(block.get_unique_users()),
        },
    )

    try:
        async with AsyncSessionLocal() as session:
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
                        user = await user_service.get_user(user_id)
                        if user and user.nickname:
                            user_names[user_id] = user.nickname
                        else:
                            user_names[user_id] = f"用户{user_id}"
                except Exception:
                    user_names[user_id] = f"用户{user_id}"

            context_manager = ContextManager(session)
            context = await context_manager.get_recent_context(
                group_id=group_id,
                limit=50,  # Layer 1: 使用50条历史消息提供深层话题背景
                bot_id=block.messages[0].event.self_id if block.messages else None,
            )

        msg = f"[group_chat] 🧠 第一层AI判断块内容..."
        logger.info(msg, extra={"group_id": group_id})

        judge_result = await block_judger.judge_block(
            block=block,
            context=context,
            group_id=group_id,
            user_names=user_names,
        )

        if not judge_result.replies:
            msg = f"[group_chat] ❌ 不需要回复 | 群={group_id}, 原因={judge_result.explanation}"
            logger.info(msg, extra={"group_id": group_id})
            return

        msg = f"[group_chat] ✅ 准备生成回复 | 群={group_id}, 需要{len(judge_result.replies)}条回复"
        logger.info(msg, extra={"group_id": group_id, "reply_count": len(judge_result.replies)})

        can_send = await message_aggregator.wait_for_reply_quiet_window(
            group_id=group_id,
            block=block,
            quiet_seconds=2.0,
        )
        if not can_send:
            logger.info(
                f"[group_chat] ⏸️ 发送前2秒静默窗口内出现新消息，取消本次发送 | 群={group_id}",
                extra={"group_id": group_id},
            )
            return

        for i, reply_plan in enumerate(judge_result.replies):
            msg = f"[group_chat] 🔷 正在生成第 {i + 1}/{len(judge_result.replies)} 条回复 | 态度={reply_plan.emotion}, @用户={reply_plan.target_user_id}"
            logger.info(msg, extra={
                "group_id": group_id,
                "reply_index": i + 1,
                "emotion": reply_plan.emotion,
                "target_user_id": reply_plan.target_user_id,
            })

            legacy_judge_result = JudgeResult(
                should_reply=True,
                reply_type="general",
                target_user_id=reply_plan.target_user_id,
                emotion=reply_plan.emotion,
                instruction=reply_plan.instruction,
                should_mention=reply_plan.should_mention,
            )

            block_context = f"{context}\n\n【当前对话块摘要】\n{judge_result.block_summary}\n\n【相关消息】\n{reply_plan.related_messages}"

            if reply_plan.need_image_parsing:
                async with AsyncSessionLocal() as session:
                    image_service = ImageParsingService(session)
                    refs = []
                    for pending_message in block.messages:
                        refs.extend(
                            image_service.extract_refs_from_formatted_message(
                                pending_message.formatted_message,
                                timestamp=getattr(pending_message.event, "time", None),
                            )
                        )

                    if refs:
                        await image_service.reparse_file_hashes(
                            group_id=group_id,
                            refs=refs,
                        )
                        await session.commit()
                        refresh_targets = [
                            context,
                            judge_result.block_summary,
                            reply_plan.related_messages,
                            *[
                                pending_message.formatted_message
                                for pending_message in block.messages
                            ],
                        ]
                        refreshed_texts = await image_service.refresh_multiple_texts(
                            refresh_targets
                        )
                        refreshed_context = refreshed_texts[0]
                        refreshed_summary = refreshed_texts[1]
                        refreshed_related = refreshed_texts[2]
                        refreshed_block_parts = refreshed_texts[3:]
                        refreshed_block = "\n".join(refreshed_block_parts)
                        block_context = (
                            f"{refreshed_context}\n\n【当前对话块摘要】\n{refreshed_summary}"
                            f"\n\n【当前对话块（最新图片描述）】\n{refreshed_block}"
                            f"\n\n【相关消息】\n{refreshed_related}"
                        )

            response = await conversation_service.generate_response(
                session=None,
                context=block_context,
                judge_result=legacy_judge_result,
                group_id=group_id,
            )

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

            try:
                bot_self_id = (
                    block.messages[0].event.self_id if block.messages else None
                )
                if bot_self_id:
                    onebot_message_id = _extract_sent_message_id(send_result)
                    async with AsyncSessionLocal() as session:
                        message_service = GroupMessageService(session)
                        formatted = message_converter.wrap_plain_text(
                            response,
                            user_id=bot_self_id,
                            display_name=BOT_DISPLAY_NAME,
                        )
                        await message_service.save_message(
                            group_id=group_id,
                            user_id=bot_self_id,
                            onebot_message_id=onebot_message_id,
                            raw_message=response,
                            formatted_message=formatted,
                            timestamp=china_now(),
                        )
                        await session.commit()
            except Exception as e:
                logger.warning(
                    f"[group_chat] Failed to save bot response: {e}",
                    extra={"group_id": group_id},
                )

            if i < len(judge_result.replies) - 1:
                msg = f"[group_chat] ⏳ 等待1秒后发送下一条回复 | 群={group_id}"
                logger.debug(msg, extra={"group_id": group_id})
                await asyncio.sleep(1.0)

        msg = f"[group_chat] 🎉 对话块处理完成 | 群={group_id}, 共发送{len(judge_result.replies)}条回复"
        logger.info(msg, extra={
            "group_id": group_id,
            "total_replies": len(judge_result.replies),
        })

    except Exception as e:
        logger.error(
            f"[group_chat] Failed to process block for group {group_id}: {e}",
            exc_info=True,
        )


# Register the callback with the aggregator
message_aggregator.set_reply_callback(_process_response_block)


@group_chat_handler.handle()
async def handle_group_chat(bot: Bot, event: GroupMessageEvent) -> None:
    _ = event
    _remember_bot(bot)

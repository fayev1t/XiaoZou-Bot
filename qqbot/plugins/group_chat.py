"""Group chat AI conversation plugin with message aggregation."""

import asyncio
import importlib

nonebot_module = importlib.import_module("nonebot")
adapters_module = importlib.import_module("nonebot.adapters")
onebot_v11_module = importlib.import_module("nonebot.adapters.onebot.v11")
rule_module = importlib.import_module("nonebot.rule")

on_message = nonebot_module.on_message
Event = adapters_module.Event
Bot = onebot_v11_module.Bot
GroupMessageEvent = onebot_v11_module.GroupMessageEvent
Rule = rule_module.Rule

from qqbot.core.database import AsyncSessionLocal
from qqbot.core.logging import get_logger
from qqbot.core.settings import get_primary_bot_name
from qqbot.core.time import china_now
from qqbot.services.context import ContextManager
from qqbot.services.conversation import conversation_service
from qqbot.services.image_parsing import ImageParsingService, ImageReference
from qqbot.services.group_message import GroupMessageService
from qqbot.services.message_converter import MessageConverter
from qqbot.services.message_aggregator import ResponseBlock, message_aggregator
from qqbot.services.block_judge import (
    block_judger,
    build_layer3_context,
    format_block_messages,
)
from qqbot.services.reply_plan_images import resolve_reply_plan_image_refs

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


def _collect_block_image_refs(block: ResponseBlock) -> list[ImageReference]:
    refs: list[ImageReference] = []
    for pending_message in block.messages:
        refs.extend(
            ImageParsingService.extract_refs_from_formatted_message(
                pending_message.formatted_message,
            )
        )
    return refs


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
            context_manager = ContextManager(session)
            context = await context_manager.get_recent_context(
                group_id=group_id,
                limit=50,  # Layer 2: 使用50条历史消息提供深层话题背景
                bot_id=block.messages[0].event.self_id if block.messages else None,
            )

        msg = f"[group_chat] 🧠 第二层AI判断块内容..."
        logger.info(msg, extra={"group_id": group_id})

        judge_result = await block_judger.judge_block(
            block=block,
            context=context,
            group_id=group_id,
        )

        planned_replies = [
            reply_plan for reply_plan in judge_result.replies if reply_plan.should_reply
        ]
        if not planned_replies:
            msg = (
                f"[group_chat] ❌ 不需要实际回复 | 群={group_id}, "
                f"主题数={judge_result.topic_count}, 原因={judge_result.explanation}"
            )
            logger.info(msg, extra={"group_id": group_id})
            return

        msg = (
            f"[group_chat] ✅ 准备生成回复 | 群={group_id}, "
            f"主题数={judge_result.topic_count}, 需要发送{len(planned_replies)}条回复"
        )
        logger.info(
            msg,
            extra={
                "group_id": group_id,
                "reply_count": len(planned_replies),
                "topic_count": judge_result.topic_count,
            },
        )

        block_refs = _collect_block_image_refs(block)
        current_block_text = format_block_messages(block)

        for i, reply_plan in enumerate(planned_replies):
            msg = (
                f"[group_chat] 🔷 正在生成第 {i + 1}/{len(planned_replies)} 条回复 "
                f"| @用户={reply_plan.target_user_id}, 需要@={reply_plan.should_mention}"
            )
            logger.info(
                msg,
                extra={
                    "group_id": group_id,
                    "reply_index": i + 1,
                    "target_user_id": reply_plan.target_user_id,
                    "should_mention": reply_plan.should_mention,
                },
            )

            reply_refs = resolve_reply_plan_image_refs(
                block_refs,
                reply_plan.related_image_hashes,
            )
            reply_image_inputs: list[dict[str, object]] | None = None

            if reply_plan.related_image_hashes and not reply_refs:
                logger.info(
                    "[group_chat] related_image_hashes 未命中当前对话块图片，按纯文本继续",
                    extra={
                        "group_id": group_id,
                        "reply_index": i + 1,
                        "requested_hashes": reply_plan.related_image_hashes,
                    },
                )

            if reply_refs:
                async with AsyncSessionLocal() as session:
                    image_service = ImageParsingService(session)
                    try:
                        reply_image_inputs = await image_service.build_openai_image_blocks(
                            reply_refs
                        )
                    except Exception as exc:
                        logger.warning(
                            "[group_chat] failed to build reply image payload, fallback to text-only",
                            extra={
                                "group_id": group_id,
                                "reply_index": i + 1,
                                "image_count": len(reply_refs),
                                "error": str(exc),
                            },
                        )

                if not reply_image_inputs:
                    reply_image_inputs = None

            response = await conversation_service.generate_response(
                session=None,
                context=build_layer3_context(
                    context=context,
                    current_block_text=current_block_text,
                ),
                instruction=reply_plan.instruction,
                target_user_id=reply_plan.target_user_id,
                should_mention=reply_plan.should_mention,
                group_id=group_id,
                image_inputs=reply_image_inputs,
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

            if i < len(planned_replies) - 1:
                msg = f"[group_chat] ⏳ 等待1秒后发送下一条回复 | 群={group_id}"
                logger.debug(msg, extra={"group_id": group_id})
                await asyncio.sleep(1.0)

        msg = f"[group_chat] 🎉 对话块处理完成 | 群={group_id}, 共发送{len(planned_replies)}条回复"
        logger.info(
            msg,
            extra={
                "group_id": group_id,
                "total_replies": len(planned_replies),
            },
        )

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

import asyncio
import importlib
from typing import Any

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
from qqbot.core.ids import new_msg_hash
from qqbot.core.logging import get_logger
from qqbot.core.settings import get_primary_bot_name
from qqbot.core.time import china_now
from qqbot.services.block_judge import block_judger, build_layer3_context
from qqbot.services.context import ContextManager
from qqbot.services.conversation import conversation_service
from qqbot.services.group_message import GroupMessageService
from qqbot.services.image_parsing import ImageParsingService
from qqbot.services.message_aggregator import ResponseBlock, message_aggregator
from qqbot.services.message_format_fallback import build_parse_failure_text
from qqbot.services.message_converter import MessageConverter
from qqbot.services.tool_manager import ToolManager

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


async def _ensure_block_messages_formatted(group_id: int, block: ResponseBlock) -> None:
    for index, pending_message in enumerate(block.messages, start=1):
        if pending_message.formatted_message is None and pending_message.format_task is None:
            pending_message.formatted_message = message_converter.wrap_plain_text(
                build_parse_failure_text("消息未完成格式化"),
                msg_hash=pending_message.msg_hash,
                user_id=pending_message.user_id,
                timestamp=pending_message.timestamp,
            )
            continue

        if pending_message.format_task is None:
            continue

        try:
            formatted_record = await pending_message.format_task
            pending_message.formatted_message = (
                formatted_record.formatted_message
                or message_converter.wrap_plain_text(
                    build_parse_failure_text("消息格式化结果为空"),
                    msg_hash=pending_message.msg_hash,
                    user_id=pending_message.user_id,
                    timestamp=pending_message.timestamp,
                )
            )
        except Exception as exc:
            logger.warning(
                "[group_chat] message format task failed, fallback to parse-failed placeholder",
                extra={
                    "group_id": group_id,
                    "message_index": index,
                    "persisted_message_id": pending_message.persisted_message_id,
                    "error": str(exc),
                },
            )
            pending_message.formatted_message = message_converter.wrap_plain_text(
                build_parse_failure_text("消息格式化异常"),
                msg_hash=pending_message.msg_hash,
                user_id=pending_message.user_id,
                timestamp=pending_message.timestamp,
            )
        finally:
            pending_message.format_task = None


async def _render_current_block_text(
    group_id: int,
    block: ResponseBlock,
    bot_id: int | None,
) -> str:
    async with AsyncSessionLocal() as session:
        context_manager = ContextManager(session)
        entries = [
            {
                "user_id": pending_message.user_id,
                "msg_hash": pending_message.msg_hash,
                "raw_message": pending_message.raw_message,
                "formatted_message": pending_message.formatted_message,
                "timestamp": pending_message.timestamp,
                "is_recalled": False,
            }
            for pending_message in block.messages
        ]
        return await context_manager.render_context_messages(
            group_id=group_id,
            messages=entries,
            bot_id=bot_id,
        )


def _build_image_parse_context(
    *,
    context: str,
    current_block_text: str,
    instruction: str,
) -> str:
    sections: list[str] = []

    normalized_context = context.strip()
    if normalized_context:
        sections.append(f"【历史上下文】\n{normalized_context}")

    normalized_block = current_block_text.strip()
    if normalized_block:
        sections.append(f"【当前对话块】\n{normalized_block}")

    normalized_instruction = instruction.strip()
    if normalized_instruction:
        sections.append(f"【当前回复任务】\n{normalized_instruction}")

    return "\n\n".join(sections) if sections else "（暂无图片解析上下文）"


async def _execute_reply_tool_calls(
    reply_plan,
    *,
    context: str,
    current_block_text: str,
) -> tuple[list[str], list[dict[str, Any]]]:
    tool_call_xmls: list[str] = []
    image_inputs: list[dict[str, Any]] = []
    if not reply_plan.tool_calls:
        return tool_call_xmls, image_inputs

    logger.info(
        "[group_chat] executing reply tool calls",
        extra={
            "tool_call_count": len(reply_plan.tool_calls),
            "target_user_id": reply_plan.target_user_id,
        },
    )

    async with AsyncSessionLocal() as session:
        tool_manager = ToolManager(session)
        image_service = ImageParsingService(session)
        image_parse_context = _build_image_parse_context(
            context=context,
            current_block_text=current_block_text,
            instruction=reply_plan.instruction,
        )
        image_file_hashes: list[str] = []
        for tool_call in reply_plan.tool_calls:
            logger.info(
                "[group_chat] tool call started",
                extra={
                    "tool": tool_call.tool,
                    "input_preview": tool_call.input[:120],
                    "msg_hash": tool_call.msg_hash,
                },
            )
            if tool_call.tool == "web_search":
                result = await tool_manager.execute_web_search(
                    msg_hash=tool_call.msg_hash,
                    query=tool_call.input,
                )
            elif tool_call.tool == "web_crawl":
                result = await tool_manager.execute_web_crawl(
                    msg_hash=tool_call.msg_hash,
                    url=tool_call.input,
                )
            elif tool_call.tool == "image_parse":
                result = await image_service.execute_image_parse_by_hash(
                    msg_hash=tool_call.msg_hash,
                    file_hash=tool_call.input,
                    context=image_parse_context,
                    reuse_existing=False,
                )
                image_file_hashes.append(tool_call.input)
            else:
                logger.warning(
                    "[group_chat] unsupported tool call skipped",
                    extra={
                        "tool": tool_call.tool,
                        "input": tool_call.input,
                        "msg_hash": tool_call.msg_hash,
                    },
                )
                continue
            tool_call_xmls.append(result.to_system_xml())
            logger.info(
                "[group_chat] tool call finished",
                extra={
                    "tool": result.tool_name,
                    "msg_hash": tool_call.msg_hash,
                    "call_hash": result.call_hash,
                    "output_length": len(result.output_data),
                },
            )
        await session.commit()
        image_inputs = image_service.build_layer3_image_inputs(image_file_hashes)

    logger.info(
        "[group_chat] reply tool calls committed",
        extra={
            "tool_result_count": len(tool_call_xmls),
            "image_input_count": len(image_inputs),
        },
    )

    return tool_call_xmls, image_inputs


async def _persist_bot_response(
    *,
    group_id: int,
    bot_self_id: int,
    response: str,
    onebot_message_id: str | None,
) -> None:
    async with AsyncSessionLocal() as session:
        message_service = GroupMessageService(session)
        msg_hash = new_msg_hash()
        formatted = message_converter.wrap_plain_text(
            response,
            msg_hash=msg_hash,
            user_id=bot_self_id,
            display_name=BOT_DISPLAY_NAME,
        )
        await message_service.save_message(
            group_id=group_id,
            user_id=bot_self_id,
            msg_hash=msg_hash,
            onebot_message_id=onebot_message_id,
            raw_message=response,
            formatted_message=formatted,
            timestamp=china_now(),
        )
        await session.commit()


async def _process_response_block(group_id: int, block: ResponseBlock) -> None:
    bot = _get_bot_for_block(block)
    if bot is None:
        logger.error("[group_chat] No bot instance available")
        return

    if block.get_message_count() == 0:
        logger.debug(f"[group_chat] Empty block for group {group_id}, skipping")
        return

    logger.info(
        f"[group_chat] ══════ 开始处理对话块 ══════ 群={group_id}, 消息数={block.get_message_count()}, 用户数={len(block.get_unique_users())}",
        extra={
            "group_id": group_id,
            "message_count": block.get_message_count(),
            "unique_users": len(block.get_unique_users()),
        },
    )

    try:
        await _ensure_block_messages_formatted(group_id, block)

        before_message_id = block.get_earliest_persisted_message_id()
        bot_id = block.messages[0].event.self_id if block.messages else None
        async with AsyncSessionLocal() as session:
            context_manager = ContextManager(session)
            context = await context_manager.get_recent_context(
                group_id=group_id,
                limit=50,
                bot_id=bot_id,
                before_message_id=before_message_id,
            )

        current_block_text = await _render_current_block_text(group_id, block, bot_id)

        logger.info("[group_chat] 🧠 第二层AI判断块内容...", extra={"group_id": group_id})
        judge_result = await block_judger.judge_block(
            block=block,
            context=context,
            group_id=group_id,
            current_block_text=current_block_text,
        )

        planned_replies = [
            reply_plan for reply_plan in judge_result.replies if reply_plan.should_reply
        ]
        if not planned_replies:
            logger.info(
                f"[group_chat] ❌ 不需要实际回复 | 群={group_id}, 主题数={judge_result.topic_count}, 原因={judge_result.explanation}",
                extra={"group_id": group_id},
            )
            return

        logger.info(
            f"[group_chat] ✅ 准备生成回复 | 群={group_id}, 主题数={judge_result.topic_count}, 需要发送{len(planned_replies)}条回复",
            extra={
                "group_id": group_id,
                "reply_count": len(planned_replies),
                "topic_count": judge_result.topic_count,
            },
        )

        for index, reply_plan in enumerate(planned_replies, start=1):
            logger.info(
                f"[group_chat] 🔷 正在生成第 {index}/{len(planned_replies)} 条回复 | @用户={reply_plan.target_user_id}, 需要@={reply_plan.should_mention}",
                extra={
                    "group_id": group_id,
                    "reply_index": index,
                    "target_user_id": reply_plan.target_user_id,
                    "should_mention": reply_plan.should_mention,
                },
            )

            tool_call_xmls, image_inputs = await _execute_reply_tool_calls(
                reply_plan,
                context=context,
                current_block_text=current_block_text,
            )
            logger.info(
                "[group_chat] Layer3 context ready",
                extra={
                    "group_id": group_id,
                    "tool_result_count": len(tool_call_xmls),
                    "image_input_count": len(image_inputs),
                    "context_length": len(context),
                    "current_block_length": len(current_block_text),
                },
            )
            response = await conversation_service.generate_response(
                session=None,
                context=build_layer3_context(
                    context=context,
                    current_block_text=current_block_text,
                    tool_call_xmls=tool_call_xmls,
                ),
                instruction=reply_plan.instruction,
                target_user_id=reply_plan.target_user_id,
                should_mention=reply_plan.should_mention,
                group_id=group_id,
                image_inputs=image_inputs,
            )

            send_result = await bot.send_group_msg(group_id=group_id, message=response)
            logger.info(
                f"[group_chat] 📤 第 {index} 条回复已发送 | 群={group_id}, 长度={len(response)}, 内容={response[:50]}",
                extra={
                    "group_id": group_id,
                    "reply_index": index,
                    "response_length": len(response),
                },
            )

            try:
                if bot_id:
                    onebot_message_id = _extract_sent_message_id(send_result)
                    await _persist_bot_response(
                        group_id=group_id,
                        bot_self_id=bot_id,
                        response=response,
                        onebot_message_id=onebot_message_id,
                    )
            except Exception as exc:
                logger.warning(
                    f"[group_chat] Failed to save bot response: {exc}",
                    extra={"group_id": group_id},
                )

            if index < len(planned_replies):
                await asyncio.sleep(1.0)

        logger.info(
            f"[group_chat] 🎉 对话块处理完成 | 群={group_id}, 共发送{len(planned_replies)}条回复",
            extra={"group_id": group_id, "total_replies": len(planned_replies)},
        )

    except Exception as exc:
        logger.error(
            f"[group_chat] Failed to process block for group {group_id}: {exc}",
            exc_info=True,
        )


message_aggregator.set_reply_callback(_process_response_block)


@group_chat_handler.handle()
async def handle_group_chat(bot: Bot, event: GroupMessageEvent) -> None:
    _ = event
    _remember_bot(bot)

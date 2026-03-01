"""Friend request auto-approve and private chat guidance."""

from nonebot import on_message, on_request
from nonebot.adapters import Event
from nonebot.rule import Rule
from nonebot.adapters.onebot.v11 import (
    Bot,
    FriendRequestEvent,
    PrivateMessageEvent,
)

from qqbot.core.logging import get_logger

logger = get_logger(__name__)


def _is_friend_request(event: Event) -> bool:
    return isinstance(event, FriendRequestEvent)


def _is_private_message(event: Event) -> bool:
    return isinstance(event, PrivateMessageEvent)


friend_request_handler = on_request(
    rule=Rule(_is_friend_request),
    priority=5,
    block=True,
)


@friend_request_handler.handle()
async def handle_friend_request(bot: Bot, event: FriendRequestEvent) -> None:
    """Auto-approve friend requests."""
    try:
        await bot.set_friend_add_request(flag=event.flag, approve=True)
        logger.info(
            "Friend request approved",
            extra={
                "user_id": getattr(event, "user_id", None),
                "comment": getattr(event, "comment", None),
            },
        )
    except Exception as exc:
        logger.error(
            "Failed to approve friend request: %s",
            exc,
            extra={"user_id": getattr(event, "user_id", None)},
        )


private_message_handler = on_message(
    rule=Rule(_is_private_message),
    priority=5,
    block=True,
)


@private_message_handler.handle()
async def handle_private_message(bot: Bot, event: PrivateMessageEvent) -> None:
    """Reply to private messages with group-only guidance."""
    if event.user_id == event.self_id:
        return

    reply = "请把我拉入群聊，AI聊天只在群内启动。"
    try:
        await bot.send(event, message=reply)
        logger.info(
            "Replied to private message",
            extra={"user_id": event.user_id},
        )
    except Exception as exc:
        logger.error(
            "Failed to reply private message: %s",
            exc,
            extra={"user_id": event.user_id},
        )

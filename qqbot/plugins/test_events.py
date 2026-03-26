"""Test script to verify if napcat pushes group member join/leave events.

This plugin tests whether the OneBot adapter receives the following events:
- GroupIncreaseNoticeEvent: When a member joins the group
- GroupDecreaseNoticeEvent: When a member leaves the group
- GroupRecallNoticeEvent: When a message is recalled

Usage:
1. Invite a user to the test group (should trigger GroupIncreaseNoticeEvent)
2. Remove a user from the test group (should trigger GroupDecreaseNoticeEvent)
3. Recall a message in the test group (should trigger GroupRecallNoticeEvent)
4. Send a normal message (sanity check)

Check console/logs for event information.
"""

import os

from nonebot import on_notice, on_message
from nonebot.adapters import Event
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupIncreaseNoticeEvent,
    GroupDecreaseNoticeEvent,
    GroupRecallNoticeEvent,
    MessageEvent,
)
from nonebot.rule import Rule

from qqbot.core.logging import get_logger

logger = get_logger(__name__)
TEST_EVENTS_ENABLED = os.getenv("QQBOT_ENABLE_TEST_EVENTS", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


def _is_group_increase_notice(event: Event) -> bool:
    return isinstance(event, GroupIncreaseNoticeEvent)


def _is_group_decrease_notice(event: Event) -> bool:
    return isinstance(event, GroupDecreaseNoticeEvent)


def _is_group_recall_notice(event: Event) -> bool:
    return isinstance(event, GroupRecallNoticeEvent)

if TEST_EVENTS_ENABLED:
    group_increase_handler = on_notice(
        rule=Rule(_is_group_increase_notice),
        priority=1,
        block=False,
    )


    @group_increase_handler.handle()
    async def handle_group_increase(
        bot: Bot,
        event: GroupIncreaseNoticeEvent,
    ) -> None:
        _ = bot
        logger.info(
            "✅ GROUP INCREASE EVENT RECEIVED",
            extra={
                "event_type": "GroupIncreaseNoticeEvent",
                "group_id": event.group_id,
                "user_id": event.user_id,
                "operator_id": event.operator_id,
                "timestamp": event.time,
            },
        )


    group_decrease_handler = on_notice(
        rule=Rule(_is_group_decrease_notice),
        priority=1,
        block=False,
    )


    @group_decrease_handler.handle()
    async def handle_group_decrease(
        bot: Bot,
        event: GroupDecreaseNoticeEvent,
    ) -> None:
        _ = bot
        logger.info(
            "✅ GROUP DECREASE EVENT RECEIVED",
            extra={
                "event_type": "GroupDecreaseNoticeEvent",
                "group_id": event.group_id,
                "user_id": event.user_id,
                "operator_id": event.operator_id,
                "timestamp": event.time,
            },
        )


    group_recall_handler = on_notice(
        rule=Rule(_is_group_recall_notice),
        priority=1,
        block=False,
    )


    @group_recall_handler.handle()
    async def handle_group_recall(bot: Bot, event: GroupRecallNoticeEvent) -> None:
        _ = bot
        logger.info(
            "✅ GROUP RECALL EVENT RECEIVED",
            extra={
                "event_type": "GroupRecallNoticeEvent",
                "group_id": event.group_id,
                "message_id": event.message_id,
                "user_id": event.user_id,
                "operator_id": event.operator_id,
                "timestamp": event.time,
            },
        )


    message_handler = on_message(priority=100, block=False)


    @message_handler.handle()
    async def handle_message(event: MessageEvent) -> None:
        if str(event.message).strip():
            logger.info(
                "✅ MESSAGE RECEIVED (sanity check)",
                extra={
                    "event_type": "MessageEvent",
                    "group_id": getattr(event, "group_id", None),
                    "user_id": event.user_id,
                    "message": str(event.message)[:100],
                },
            )
else:
    logger.info("[test_events] Plugin loaded in disabled mode")

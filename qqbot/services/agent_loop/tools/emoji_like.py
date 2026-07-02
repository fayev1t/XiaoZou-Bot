"""EmojiLikeTool —— 给某条消息贴上 / 取消 QQ 表情回应（表情回应）。

仅 GroupAgentLoop 可用（allowed_scopes=("group",)）。本工具操作的是「某条
消息」——napcat 的 set_msg_emoji_like 只凭 message_id 即可定位，**不需要
group_id**。仍调 require_group_scope 校验当前确在群里，但不把 group_id 传给
napcat。

message_id 从 timeline 里消息渲染的 ``<message ... id="MESSAGE_ID">`` 取，
其 id 即 onebot_message_id（整数）。

注意：``set`` 是 Python 内置名，本工具局部变量用 set_flag；但传给 napcat 的
参数名仍是 set（set=set_flag）。

napcat 动作失败（表情 id 非法等）由 call_action 折成 upstream_action_failed
**返回**；权限/角色/scope 判定在 execute() 首行 enforce_access（先于任何 napcat
动作）返回对应失败 outcome。全程无 raise。

权限：required_permission 用 BaseTool 默认 GUEST —— 贴表情回应是轻量互动。

OneBot action：set_msg_emoji_like(message_id, emoji_id, set)。
"""

from __future__ import annotations

from typing import Any

from qqbot.core.logging import get_logger
from qqbot.services.agent_loop.prompts import load_sibling_md
from qqbot.services.agent_loop.tool_registry import BaseTool, ToolOutcome
from qqbot.services.agent_loop.tools._onebot_common import (
    call_action,
    coerce_bool,
    coerce_int,
    get_bot,
    require_group_scope,
)

logger = get_logger(__name__)

_USAGE_PROMPT = load_sibling_md(__file__, "emoji_like.md")


class EmojiLikeTool(BaseTool):
    name = "emoji_like"
    allowed_scopes = ("group",)
    # required_permission 用 BaseTool 默认 GUEST
    # required_bot_role 不设：贴表情回应是轻量互动，bot 无需群管理员。
    usage_prompt = _USAGE_PROMPT
    description = (
        "React to a single message with a QQ emoji (表情回应) in the CURRENT "
        "group. Pass message_id — the onebot_message_id of the target "
        "message — which you read from a <message ... id=\"MESSAGE_ID\"> row "
        "in the timeline (the id attribute IS the onebot_message_id) — "
        "emoji_id (the QQ emoji/face id to react with; number or string), and "
        "set (true = add the reaction, the default; false = remove a reaction "
        "you previously added)."
    )
    arguments_schema = {
        "type": "object",
        "properties": {
            "message_id": {
                "type": "integer",
                "description": (
                    "onebot_message_id of the target message, taken from a "
                    "<message ... id=\"MESSAGE_ID\"> row in the timeline."
                ),
            },
            "emoji_id": {
                "type": "string",
                "description": (
                    "QQ emoji/face id to react with (number or string)."
                ),
            },
            "set": {
                "type": "boolean",
                "description": (
                    "true = add the emoji reaction (default); false = remove "
                    "it."
                ),
                "default": True,
            },
        },
        "required": ["message_id", "emoji_id"],
    }

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        if fail := await self.enforce_access(context):
            return fail
        # 校验当前确在群里；message 操作只凭 message_id，不把 group_id 传给
        # napcat。
        _gid, fail = require_group_scope(context, self.name)
        if fail:
            return fail
        message_id, fail = coerce_int(
            arguments.get("message_id"), f"{self.name}.message_id"
        )
        if fail:
            return fail
        # emoji_id 可能是数字或字符串，统一转字符串并校验非空。
        raw_emoji = arguments.get("emoji_id")
        emoji_id = str(raw_emoji) if raw_emoji is not None else ""
        if not emoji_id:
            return ToolOutcome.failure(
                "invalid_arguments", f"{self.name}.emoji_id is required"
            )
        # set 是 Python 内置名，局部用 set_flag；传 napcat 时参数名仍是 set。
        set_flag, fail = coerce_bool(
            arguments.get("set"), f"{self.name}.set", default=True
        )
        if fail:
            return fail

        bot, fail = get_bot()
        if fail:
            return fail
        _, fail = await call_action(
            bot,
            "set_msg_emoji_like",
            message_id=message_id,
            emoji_id=emoji_id,
            set=set_flag,
        )
        if fail:
            return fail
        logger.info(
            "[{}] message_id={} emoji_id={} set={}",
            self.name,
            message_id,
            emoji_id,
            set_flag,
        )
        return ToolOutcome.success(
            message_id=message_id,
            emoji_id=emoji_id,
            set=set_flag,
        )

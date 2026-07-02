"""SetEssenceTool —— 把某条消息设为/取消群精华消息。

仅 GroupAgentLoop 可用（allowed_scopes=("group",)）。本工具操作的是「某条
消息」——napcat 的 set_essence_msg / delete_essence_msg 只凭 message_id 即可
定位，**不需要 group_id**。仍调 require_group_scope 校验当前确在群里（精华
是群里的消息），但不把 group_id 传给 napcat。

message_id 从 timeline 里消息渲染的 ``<message ... id="MESSAGE_ID">`` 取，
其 id 即 onebot_message_id（整数）。

napcat 动作失败（bot 权限不足 / 消息不存在等）由 call_action 折成
upstream_action_failed **返回**；权限/角色/scope 判定在 execute() 首行
enforce_access（先于任何 napcat 动作）返回对应失败 outcome。全程无 raise。

权限：required_permission=OWNER —— 群精华是全群可见的高曝光操作，须群主授意。

OneBot action：set_essence_msg(message_id) / delete_essence_msg(message_id)。
"""

from __future__ import annotations

from typing import Any

from qqbot.core.logging import get_logger
from qqbot.core.permissions import PermissionTier
from qqbot.services.agent_loop.prompts import load_sibling_md
from qqbot.services.agent_loop.tool_registry import BaseTool, ToolOutcome
from qqbot.services.agent_loop.tools._onebot_common import (
    call_action,
    coerce_int,
    get_bot,
    require_group_scope,
)

logger = get_logger(__name__)

_USAGE_PROMPT = load_sibling_md(__file__, "set_essence.md")


class SetEssenceTool(BaseTool):
    name = "set_essence"
    allowed_scopes = ("group",)
    required_permission = PermissionTier.OWNER
    required_bot_role = "admin"  # set_essence_msg 需要 bot 自己是群管理员
    usage_prompt = _USAGE_PROMPT
    description = (
        "Set or unset a single message as a group ESSENCE message (群精华). "
        "Pass message_id — the onebot_message_id of the target message — which "
        "you read from a <message ... id=\"MESSAGE_ID\"> row in the timeline "
        "(the id attribute IS the onebot_message_id) — and action: \"set\" "
        "(default) to add it to the group essence list, or \"delete\" to "
        "remove it. This is a high-visibility group-wide action; requires the "
        "bot itself to be a group admin/owner — if it isn't, the call fails "
        "and you'll see the reason next tick."
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
            "action": {
                "type": "string",
                "enum": ["set", "delete"],
                "description": (
                    "\"set\" to mark the message as essence (default); "
                    "\"delete\" to remove it from the essence list."
                ),
                "default": "set",
            },
        },
        "required": ["message_id"],
    }

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        if fail := await self.enforce_access(context):
            return fail
        # 校验当前确在群里（精华是群里的消息）；message 操作只凭 message_id，
        # 不把 group_id 传给 napcat。
        _gid, fail = require_group_scope(context, self.name)
        if fail:
            return fail
        message_id, fail = coerce_int(
            arguments.get("message_id"), f"{self.name}.message_id"
        )
        if fail:
            return fail
        action = arguments.get("action", "set")

        bot, fail = get_bot()
        if fail:
            return fail
        if action == "set":
            _, fail = await call_action(
                bot, "set_essence_msg", message_id=message_id
            )
        elif action == "delete":
            _, fail = await call_action(
                bot, "delete_essence_msg", message_id=message_id
            )
        else:
            return ToolOutcome.failure(
                "invalid_arguments",
                f"{self.name}.action must be 'set' or 'delete', got {action!r}",
            )
        if fail:
            return fail
        logger.info(
            "[{}] message_id={} action={}", self.name, message_id, action
        )
        return ToolOutcome.success(message_id=message_id, action=action)

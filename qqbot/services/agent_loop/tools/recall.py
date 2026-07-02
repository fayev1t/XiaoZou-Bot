"""RecallTool —— 撤回当前群里的某条消息。

仅 GroupAgentLoop 可用（allowed_scopes=("group",)）。本工具操作的是「某条
消息」——napcat 的 delete_msg 只凭 message_id 即可定位，**不需要 group_id**。
仍调 require_group_scope 校验当前确在群里（撤的是群消息），但不把 group_id
传给 napcat。

message_id 从 timeline 里消息渲染的 ``<message ... id="MESSAGE_ID">`` 取，
其 id 即 onebot_message_id（整数）。

撤别人的消息需 bot 自己是群管理员——napcat 动作失败（bot 权限不足 / 消息不存在
等）由 call_action 折成 upstream_action_failed **返回**；权限/角色/scope 判定在
execute() 首行 enforce_access（先于任何 napcat 动作）返回对应失败 outcome。
全程无 raise。

权限：required_permission 用 BaseTool 默认 GUEST —— 撤自己的消息随时可以，
撤别人的由 bot 是否为群管理员把关，不在工具层加额外 tier 门禁。

OneBot action：delete_msg(message_id)。
"""

from __future__ import annotations

from typing import Any

from qqbot.core.logging import get_logger
from qqbot.services.agent_loop.prompts import load_sibling_md
from qqbot.services.agent_loop.tool_registry import BaseTool, ToolOutcome
from qqbot.services.agent_loop.tools._onebot_common import (
    call_action,
    coerce_int,
    enforce_actor_outranks_target,
    fetch_message_author,
    get_bot,
    require_group_scope,
    role_rank,
)

logger = get_logger(__name__)

_USAGE_PROMPT = load_sibling_md(__file__, "recall.md")


class RecallTool(BaseTool):
    name = "recall"
    allowed_scopes = ("group",)
    # required_permission 用 BaseTool 默认 GUEST
    # required_bot_role 不设：撤自己的消息无需 admin，撤别人的由 napcat 把关。
    usage_prompt = _USAGE_PROMPT
    description = (
        "Recall (delete) a single message in the CURRENT group. Pass "
        "message_id — the onebot_message_id of the message to recall — which "
        "you read from a <message ... id=\"MESSAGE_ID\"> row in the timeline "
        "(the id attribute IS the onebot_message_id). Recalling your OWN "
        "message always works; recalling SOMEONE ELSE's message requires the "
        "bot itself to be a group admin — if it isn't, the call fails and "
        "you'll see the reason next tick."
    )
    arguments_schema = {
        "type": "object",
        "properties": {
            "message_id": {
                "type": "integer",
                "description": (
                    "onebot_message_id of the message to recall, taken from a "
                    "<message ... id=\"MESSAGE_ID\"> row in the timeline."
                ),
            },
        },
        "required": ["message_id"],
    }

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        if fail := await self.enforce_access(context):
            return fail
        # 校验当前确在群里（撤的是群消息）；message 操作只凭 message_id，
        # 不把 group_id 传给 napcat。
        _gid, fail = require_group_scope(context, self.name)
        if fail:
            return fail
        message_id, fail = coerce_int(
            arguments.get("message_id"), f"{self.name}.message_id"
        )
        if fail:
            return fail

        bot, fail = get_bot()
        if fail:
            return fail
        # 细粒度前置判定：撤**自己**发的消息随时可以；撤**别人**的须 bot 是群管理员/
        # 群主，且不能撤 ≥ 自己角色者（admin 撤不了群主/另一 admin 的消息）。作者/角色
        # 查不到（get_msg 不可用等）就不前置拦，交 napcat 兜底——保持"撤消息只凭
        # message_id"的宽松默认。
        author_id, author_role = await fetch_message_author(bot, message_id)
        if author_id is not None and str(author_id) != str(
            getattr(bot, "self_id", "")
        ):
            # bot 角色实时解析（撤他人消息才需要，故懒查；查不到回退快照）。
            bot_role = await self._effective_bot_role(context)
            if role_rank(bot_role) < role_rank("admin"):
                return ToolOutcome.failure(
                    "permission_denied_bot_role",
                    f"{self.name} can only recall someone else's message when "
                    f"the bot is a group admin/owner; current "
                    f"bot_role={bot_role or 'unknown'}",
                    required_bot_role="admin",
                    actual_bot_role=bot_role or None,
                    target_role=author_role,
                )
            if fail := enforce_actor_outranks_target(
                self.name,
                "recall a message from",
                bot_role,
                author_role,
                author_id,
            ):
                return fail
        _, fail = await call_action(bot, "delete_msg", message_id=message_id)
        if fail:
            return fail
        logger.info("[{}] message_id={}", self.name, message_id)
        return ToolOutcome.success(message_id=message_id)

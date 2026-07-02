"""SetCardTool —— 修改当前群某个成员的群名片。

仅 GroupAgentLoop 可用（allowed_scopes=("group",)）；group_id 锁定当前群——
从 scope_key 注入、不由 LLM 传（隔离契约 §9：一个群的 agent 不能操作别的群）。
napcat 动作失败（bot 权限不足 / 目标不存在等）由 call_action 折成
upstream_action_failed **返回**；权限/角色/scope 判定在 execute() 首行
enforce_access（先于任何 napcat 动作）返回对应失败 outcome。全程无 raise。

权限：required_permission=ADMIN —— 改他人群名片须群管理员/群主在场授意。

OneBot action：set_group_card(group_id, user_id, card)。
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
    enforce_actor_outranks_target,
    fetch_member_role,
    get_bot,
    require_group_scope,
)

logger = get_logger(__name__)

_USAGE_PROMPT = load_sibling_md(__file__, "set_card.md")


class SetCardTool(BaseTool):
    name = "set_card"
    allowed_scopes = ("group",)
    required_permission = PermissionTier.ADMIN
    required_bot_role = "admin"  # set_group_card 需要 bot 自己是群管理员
    usage_prompt = _USAGE_PROMPT
    description = (
        "Set the group nickname / card (群名片) of a member in the CURRENT "
        "group. Operates on the current group only — group_id comes from your "
        "scope, you cannot edit cards in another group. Pass user_id (the QQ "
        "number — read it from a <message sender_id=\"USER_ID\"> in the "
        "timeline) and card (the new display name); pass card=\"\" (empty) to "
        "CLEAR the card. This is a sensitive moderation action; requires the "
        "bot itself to be a group admin/owner — if it isn't, the call fails "
        "and you'll see the reason next tick."
    )
    arguments_schema = {
        "type": "object",
        "properties": {
            "user_id": {
                "type": "integer",
                "description": "QQ number of the member whose card to set.",
            },
            "card": {
                "type": "string",
                "description": (
                    "New group card text. Empty string clears the card."
                ),
                "default": "",
            },
        },
        "required": ["user_id"],
    }

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        if fail := await self.enforce_access(context):
            return fail
        group_id, fail = require_group_scope(context, self.name)
        if fail:
            return fail
        user_id, fail = coerce_int(
            arguments.get("user_id"), f"{self.name}.user_id"
        )
        if fail:
            return fail
        card = str(arguments.get("card") or "")

        bot, fail = get_bot()
        if fail:
            return fail
        # 细粒度层级前置判定：改他人名片须 bot 角色高于对方（admin 改不了另一个
        # admin / 群主的名片）。改**自己**的名片随时可以 → 目标是 bot 自身时跳过。
        # 目标角色实时查；查不到不拦，交 napcat 兜底（见 _onebot_common 注释）。
        if str(getattr(bot, "self_id", "")) != str(user_id):
            bot_role = await self._effective_bot_role(context)
            target_role = await fetch_member_role(bot, group_id, user_id)
            if fail := enforce_actor_outranks_target(
                self.name,
                "edit the group card of",
                bot_role,
                target_role,
                user_id,
            ):
                return fail
        _, fail = await call_action(
            bot,
            "set_group_card",
            group_id=group_id,
            user_id=user_id,
            card=card,
        )
        if fail:
            return fail
        logger.info(
            "[{}] group={} user={} card={!r}",
            self.name,
            group_id,
            user_id,
            card,
        )
        return ToolOutcome.success(
            group_id=group_id,
            user_id=user_id,
            card=card,
        )

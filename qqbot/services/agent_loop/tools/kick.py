"""KickTool — 把某个成员踢出当前群。

仅 GroupAgentLoop 可用（allowed_scopes=("group",)）；group_id 锁定当前群——
从 scope_key 注入、不由 LLM 传（隔离契约 §9：一个群的 agent 不能操作别的群）。

权限/判定全在工具内（execute() 第一行 ``await self.enforce_access``，AgentLoop 不再
闸门）：发起人 tier（required_permission=ADMIN——踢人须群管理员/群主授意，**实时**查
其当前群角色）+ bot 自身角色（required_bot_role="admin"，set_group_kick 需 bot 是群
管理员）。任一不够即**返回** permission_denied_* / tool_unavailable_in_scope，**不发起
任何 napcat 动作**。napcat 动作失败（目标不存在等）由 call_action 折成
upstream_action_failed 返回。全程无 raise。

OneBot action：set_group_kick(group_id, user_id, reject_add_request)。
"""

from __future__ import annotations

from typing import Any

from qqbot.core.logging import get_logger
from qqbot.core.permissions import PermissionTier
from qqbot.services.agent_loop.prompts import load_sibling_md
from qqbot.services.agent_loop.tool_registry import BaseTool, ToolOutcome
from qqbot.services.agent_loop.tools._onebot_common import (
    call_action,
    coerce_bool,
    coerce_int,
    enforce_actor_outranks_target,
    fetch_member_role,
    get_bot,
    require_group_scope,
)

logger = get_logger(__name__)

_USAGE_PROMPT = load_sibling_md(__file__, "kick.md")


class KickTool(BaseTool):
    name = "kick"
    allowed_scopes = ("group",)
    required_permission = PermissionTier.ADMIN
    required_bot_role = "admin"  # set_group_kick 需要 bot 自己是群管理员
    usage_prompt = _USAGE_PROMPT
    description = (
        "Remove (kick) a member from the CURRENT group. Operates on the "
        "current group only — group_id comes from your scope, you cannot kick "
        "from another group. Pass user_id (the QQ number to kick — read it "
        "from a <message sender_id=\"USER_ID\"> in the timeline) and "
        "optionally reject_add_request=true to also block their future join "
        "requests. Requires the bot itself to be a group admin; if it isn't, "
        "the call fails and you'll see the reason next tick."
    )
    arguments_schema = {
        "type": "object",
        "properties": {
            "user_id": {
                "type": "integer",
                "description": "QQ number of the member to kick.",
            },
            "reject_add_request": {
                "type": "boolean",
                "description": (
                    "If true, also reject this user's future join requests."
                ),
                "default": False,
            },
        },
        "required": ["user_id"],
    }

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        # 全程无 raise：每个 helper 返回失败 outcome，直接 return 上来。
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
        reject, fail = coerce_bool(
            arguments.get("reject_add_request"),
            f"{self.name}.reject_add_request",
            default=False,
        )
        if fail:
            return fail

        bot, fail = get_bot()
        if fail:
            return fail
        # 细粒度层级前置判定：踢不掉 ≥ 自己角色的人（admin 踢不掉群主/另一 admin）。
        # bot 角色实时解析（enforce_bot_admin 已查并缓存，这里复用）、目标角色实时查；
        # 查不到不拦，交 napcat 兜底（见 _onebot_common 注释）。
        bot_role = await self._effective_bot_role(context)
        target_role = await fetch_member_role(bot, group_id, user_id)
        if fail := enforce_actor_outranks_target(
            self.name, "kick", bot_role, target_role, user_id
        ):
            return fail
        _, fail = await call_action(
            bot,
            "set_group_kick",
            group_id=group_id,
            user_id=user_id,
            reject_add_request=reject,
        )
        if fail:
            return fail
        logger.info("[{}] group={} user={}", self.name, group_id, user_id)
        return ToolOutcome.success(group_id=group_id, user_id=user_id)

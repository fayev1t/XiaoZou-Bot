"""SetAdminTool —— 给当前群的某个成员设/取消管理员。

仅 GroupAgentLoop 可用（allowed_scopes=("group",)）；group_id 锁定当前群——
从 scope_key 注入、不由 LLM 传（隔离契约 §9：一个群的 agent 不能操作别的群）。
napcat 动作失败（bot 非群主 / 目标不存在等）由 call_action 折成
upstream_action_failed **返回**；权限/角色/scope 判定在 execute() 首行
enforce_access（先于任何 napcat 动作）返回对应失败 outcome。全程无 raise。

权限：required_permission=OWNER —— 任免管理员是群主专属权力，须群主授意。

OneBot action：set_group_admin(group_id, user_id, enable)。
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
    get_bot,
    require_group_scope,
)

logger = get_logger(__name__)

_USAGE_PROMPT = load_sibling_md(__file__, "set_admin.md")


class SetAdminTool(BaseTool):
    name = "set_admin"
    allowed_scopes = ("group",)
    required_permission = PermissionTier.OWNER
    required_bot_role = "owner"  # set_group_admin 是群主专属能力，bot 须是群主
    usage_prompt = _USAGE_PROMPT
    description = (
        "Grant or revoke group-admin (管理员) for a member of the CURRENT "
        "group. Operates on the current group only — group_id comes from your "
        "scope, you cannot change roles in another group. Pass user_id (the QQ "
        "number — read it from a <message sender_id=\"USER_ID\"> in the "
        "timeline) and enable=true to make them an admin or enable=false to "
        "revoke it. This is a sensitive privileged action; requires the bot "
        "ITSELF to be the group OWNER (群主) — if it isn't, the call fails and "
        "you'll see the reason next tick."
    )
    arguments_schema = {
        "type": "object",
        "properties": {
            "user_id": {
                "type": "integer",
                "description": "QQ number of the member to promote/demote.",
            },
            "enable": {
                "type": "boolean",
                "description": (
                    "true = make them a group admin; false = revoke admin."
                ),
                "default": True,
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
        enable, fail = coerce_bool(
            arguments.get("enable"), f"{self.name}.enable", default=True
        )
        if fail:
            return fail

        bot, fail = get_bot()
        if fail:
            return fail
        _, fail = await call_action(
            bot,
            "set_group_admin",
            group_id=group_id,
            user_id=user_id,
            enable=enable,
        )
        if fail:
            return fail
        logger.info(
            "[{}] group={} user={} enable={}",
            self.name,
            group_id,
            user_id,
            enable,
        )
        return ToolOutcome.success(
            group_id=group_id,
            user_id=user_id,
            enable=enable,
        )

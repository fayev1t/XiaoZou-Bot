"""WholeBanTool —— 开启/解除当前群的全员禁言。

仅 GroupAgentLoop 可用（allowed_scopes=("group",)）；group_id 锁定当前群——
从 scope_key 注入、不由 LLM 传（隔离契约 §9：一个群的 agent 不能操作别的群）。
napcat 动作失败（bot 权限不足等）由 call_action 折成 upstream_action_failed
**返回**；权限/角色/scope 判定在 execute() 首行 enforce_access（先于任何 napcat
动作）返回对应失败 outcome。全程无 raise。

权限：required_permission=OWNER —— 全员禁言影响整个群的所有人，须群主授意。

OneBot action：set_group_whole_ban(group_id, enable)。
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
    get_bot,
    require_group_scope,
)

logger = get_logger(__name__)

_USAGE_PROMPT = load_sibling_md(__file__, "whole_ban.md")


class WholeBanTool(BaseTool):
    name = "whole_ban"
    allowed_scopes = ("group",)
    required_permission = PermissionTier.OWNER
    required_bot_role = "admin"  # set_group_whole_ban 需要 bot 自己是群管理员
    usage_prompt = _USAGE_PROMPT
    description = (
        "Enable or lift WHOLE-GROUP mute (全员禁言) for the CURRENT group. "
        "Operates on the current group only — group_id comes from your scope, "
        "you cannot affect another group. Pass enable=true to mute everyone "
        "(no member except admins can speak), enable=false to lift the mute. "
        "This is a high-impact action affecting every member; requires the bot "
        "itself to be a group admin/owner — if it isn't, the call fails and "
        "you'll see the reason next tick."
    )
    arguments_schema = {
        "type": "object",
        "properties": {
            "enable": {
                "type": "boolean",
                "description": (
                    "true = turn on whole-group mute; false = lift it."
                ),
                "default": True,
            },
        },
        "required": [],
    }

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        # 全程无 raise：每个 helper 返回失败 outcome，直接 return 上来。
        if fail := await self.enforce_access(context):
            return fail
        group_id, fail = require_group_scope(context, self.name)
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
            bot, "set_group_whole_ban", group_id=group_id, enable=enable
        )
        if fail:
            return fail
        logger.info("[{}] group={} enable={}", self.name, group_id, enable)
        return ToolOutcome.success(group_id=group_id, enable=enable)

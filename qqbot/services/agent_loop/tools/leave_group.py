"""LeaveGroupTool —— 让 bot 退出 / 解散当前群。

仅 GroupAgentLoop 可用（allowed_scopes=("group",)）；group_id 锁定当前群——
从 scope_key 注入、不由 LLM 传（隔离契约 §9：一个群的 agent 不能操作别的群）。
napcat 动作失败（bot 权限不足等）由 call_action 折成 upstream_action_failed
**返回**；权限/角色/scope 判定在 execute() 首行 enforce_access（先于任何 napcat
动作）返回对应失败 outcome。全程无 raise。

⚠️ 高危：执行后 bot 直接退出（或在 bot 是群主且 is_dismiss=True 时解散整个群），
bot 在该群立即失效、收不到也发不出任何消息，不可逆。required_permission=OWNER。

OneBot action：set_group_leave(group_id, is_dismiss)。
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

_USAGE_PROMPT = load_sibling_md(__file__, "leave_group.md")


class LeaveGroupTool(BaseTool):
    name = "leave_group"
    allowed_scopes = ("group",)
    required_permission = PermissionTier.OWNER
    # 自己退群不需要 bot 是管理员 → 不设 required_bot_role（保持 BaseTool 默认 None）。
    usage_prompt = _USAGE_PROMPT
    description = (
        "⚠️ HIGH-RISK, IRREVERSIBLE. Make the bot LEAVE the CURRENT group "
        "(or DISMISS the whole group if the bot is its owner and "
        "is_dismiss=true). Operates on the current group only — group_id comes "
        "from your scope, you cannot affect another group. Pass is_dismiss "
        "(optional, default false): when true and the bot is the group owner, "
        "the entire group is disbanded. After this runs the bot is gone from "
        "the group and can no longer receive or send anything there; this "
        "cannot be undone — only do it on an explicit owner request."
    )
    arguments_schema = {
        "type": "object",
        "properties": {
            "is_dismiss": {
                "type": "boolean",
                "description": (
                    "If true and the bot is the group owner, dismiss "
                    "(disband) the whole group instead of just leaving."
                ),
                "default": False,
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
        is_dismiss, fail = coerce_bool(
            arguments.get("is_dismiss"), f"{self.name}.is_dismiss", default=False
        )
        if fail:
            return fail
        # 细粒度前置判定：解散整个群（is_dismiss=true）是群主专属；bot 不是群主时
        # 先拦掉，给精确 permission_denied_bot_role，而非等 napcat 返回中文 wording。
        # 普通退群（is_dismiss=false）不需要任何 bot 角色，照旧放行。
        if is_dismiss:
            # bot 角色实时解析（懒查：只有 dismiss 才需要；查不到回退快照）。
            bot_role = await self._effective_bot_role(context)
            if bot_role != "owner":
                return ToolOutcome.failure(
                    "permission_denied_bot_role",
                    f"{self.name} with is_dismiss=true dismisses the whole "
                    f"group, which requires the bot to be the group owner; "
                    f"current bot_role={bot_role or 'unknown'}",
                    required_bot_role="owner",
                    actual_bot_role=bot_role or None,
                )

        bot, fail = get_bot()
        if fail:
            return fail
        _, fail = await call_action(
            bot, "set_group_leave", group_id=group_id, is_dismiss=is_dismiss
        )
        if fail:
            return fail
        logger.info("[{}] group={} is_dismiss={}", self.name, group_id, is_dismiss)
        return ToolOutcome.success(group_id=group_id, is_dismiss=is_dismiss)

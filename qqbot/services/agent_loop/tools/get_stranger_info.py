"""GetStrangerInfoTool —— 查询任意 QQ 用户的公开陌生人资料。

不限 scope（不设 allowed_scopes）：查的是 QQ 公开资料、不依赖任何群，所以
system / group / private 各 AgentLoop 都能调，也**不**从 scope_key 取 group_id。
napcat 动作失败（查不到用户等）由 call_action 折成 upstream_action_failed
**返回**；权限/角色/scope 判定在 execute() 首行 enforce_access（先于任何 napcat
动作）返回对应失败 outcome。全程无 raise。

权限：查询无副作用，沿用 BaseTool 默认 GUEST。

返回精简后的 user_id/nickname/sex/age。

OneBot action：get_stranger_info(user_id, no_cache)。
"""

from __future__ import annotations

from typing import Any

from qqbot.core.logging import get_logger
from qqbot.services.agent_loop.prompts import load_sibling_md
from qqbot.services.agent_loop.tool_registry import BaseTool, ToolOutcome
from qqbot.services.agent_loop.tools._onebot_common import (
    call_action,
    coerce_int,
    get_bot,
)

logger = get_logger(__name__)

_USAGE_PROMPT = load_sibling_md(__file__, "get_stranger_info.md")


class GetStrangerInfoTool(BaseTool):
    name = "get_stranger_info"
    # 不设 allowed_scopes：不限 scope，查 QQ 公开资料不依赖群。
    # required_permission 用 BaseTool 默认 GUEST（查询无副作用）
    usage_prompt = _USAGE_PROMPT
    description = (
        "Look up the basic public profile of ANY QQ user by user_id, even "
        "someone who is not in this group (uses QQ's public stranger info). "
        "Works in any scope; it does NOT depend on a group. Pass user_id (the "
        "QQ number to look up). Returns user_id, nickname, sex and age. "
        "Read-only, no side effects."
    )
    arguments_schema = {
        "type": "object",
        "properties": {
            "user_id": {
                "type": "integer",
                "description": "QQ number of the user to look up.",
            },
        },
        "required": ["user_id"],
    }

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        if fail := await self.enforce_access(context):
            return fail
        # 不依赖群：不调 require_group_scope，直接取 bot + 校验 user_id。
        user_id, fail = coerce_int(
            arguments.get("user_id"), f"{self.name}.user_id"
        )
        if fail:
            return fail

        bot, fail = get_bot()
        if fail:
            return fail
        info, fail = await call_action(
            bot, "get_stranger_info", user_id=user_id, no_cache=False
        )
        if fail:
            return fail
        info = info or {}
        logger.info("[{}] user={}", self.name, user_id)
        return ToolOutcome.success({
            "user_id": info.get("user_id", user_id),
            "nickname": info.get("nickname"),
            "sex": info.get("sex"),
            "age": info.get("age"),
        })

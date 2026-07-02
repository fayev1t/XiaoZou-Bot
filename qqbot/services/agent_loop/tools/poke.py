"""PokeTool —— 戳一戳当前群里的某个成员。

仅 GroupAgentLoop 可用（allowed_scopes=("group",)）；group_id 锁定当前群——
从 scope_key 注入、不由 LLM 传（隔离契约 §9：一个群的 agent 不能操作别的群）。
napcat 动作失败（目标不存在等）由 call_action 折成 upstream_action_failed
**返回**；权限/角色/scope 判定在 execute() 首行 enforce_access（先于任何 napcat
动作）返回对应失败 outcome。全程无 raise。

权限：戳一戳是无害的轻互动，沿用 BaseTool 默认 GUEST，普通群员触发即可。

OneBot action：group_poke(group_id, user_id)。
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
    require_group_scope,
)

logger = get_logger(__name__)

_USAGE_PROMPT = load_sibling_md(__file__, "poke.md")


class PokeTool(BaseTool):
    name = "poke"
    allowed_scopes = ("group",)
    # required_permission 用 BaseTool 默认 GUEST
    # required_bot_role 用 BaseTool 默认 None —— 戳一戳无害，bot 普通成员即可
    usage_prompt = _USAGE_PROMPT
    description = (
        "Poke (戳一戳) a member in the CURRENT group. Operates on the current "
        "group only — group_id comes from your scope, you cannot poke someone "
        "in another group. Pass user_id (the QQ number to poke — read it from "
        "a <message sender_id=\"USER_ID\"> in the timeline). A light, "
        "harmless nudge that shows up as a poke animation for that member."
    )
    arguments_schema = {
        "type": "object",
        "properties": {
            "user_id": {
                "type": "integer",
                "description": "QQ number of the member to poke.",
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

        bot, fail = get_bot()
        if fail:
            return fail
        _, fail = await call_action(
            bot, "group_poke", group_id=group_id, user_id=user_id
        )
        if fail:
            return fail
        logger.info("[{}] group={} user={}", self.name, group_id, user_id)
        return ToolOutcome.success(group_id=group_id, user_id=user_id)

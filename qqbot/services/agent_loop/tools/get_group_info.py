"""GetGroupInfoTool —— 查询当前群的基本信息。

仅 GroupAgentLoop 可用（allowed_scopes=("group",)）；group_id 锁定当前群——
从 scope_key 注入、不由 LLM 传（隔离契约 §9：一个群的 agent 不能查别的群）。
无参数。napcat 动作失败（查询失败等）由 call_action 折成 upstream_action_failed
**返回**；权限/角色/scope 判定在 execute() 首行 enforce_access（先于任何 napcat
动作）返回对应失败 outcome。全程无 raise。

权限：查询无副作用，沿用 BaseTool 默认 GUEST。

返回精简后的 group_id/group_name/member_count/max_member_count。

OneBot action：get_group_info(group_id, no_cache)。
"""

from __future__ import annotations

from typing import Any

from qqbot.core.logging import get_logger
from qqbot.services.agent_loop.prompts import load_sibling_md
from qqbot.services.agent_loop.tool_registry import BaseTool, ToolOutcome
from qqbot.services.agent_loop.tools._onebot_common import (
    call_action,
    get_bot,
    require_group_scope,
)

logger = get_logger(__name__)

_USAGE_PROMPT = load_sibling_md(__file__, "get_group_info.md")


class GetGroupInfoTool(BaseTool):
    name = "get_group_info"
    allowed_scopes = ("group",)
    # required_permission 用 BaseTool 默认 GUEST（查询无副作用）
    usage_prompt = _USAGE_PROMPT
    description = (
        "Get basic info about the CURRENT group. Operates on the current "
        "group only — group_id comes from your scope, you cannot query "
        "another group. Takes no arguments. Returns group_id, group_name, "
        "member_count and max_member_count. Read-only, no side effects."
    )
    arguments_schema = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        if fail := await self.enforce_access(context):
            return fail
        group_id, fail = require_group_scope(context, self.name)
        if fail:
            return fail

        bot, fail = get_bot()
        if fail:
            return fail
        info, fail = await call_action(
            bot, "get_group_info", group_id=group_id, no_cache=False
        )
        if fail:
            return fail
        info = info or {}
        logger.info("[{}] group={}", self.name, group_id)
        return ToolOutcome.success({
            "group_id": info.get("group_id", group_id),
            "group_name": info.get("group_name"),
            "member_count": info.get("member_count"),
            "max_member_count": info.get("max_member_count"),
        })

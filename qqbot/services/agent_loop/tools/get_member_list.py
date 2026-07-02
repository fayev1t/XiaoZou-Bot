"""GetMemberListTool —— 拉取当前群的成员列表（截断防爆）。

仅 GroupAgentLoop 可用（allowed_scopes=("group",)）；group_id 锁定当前群——
从 scope_key 注入、不由 LLM 传（隔离契约 §9：一个群的 agent 不能查别的群）。
napcat 动作失败（拉取失败等）由 call_action 折成 upstream_action_failed **返回**；
权限/角色/scope 判定在 execute() 首行 enforce_access（先于任何 napcat 动作）返回
对应失败 outcome。全程无 raise。

权限：查询无副作用，沿用 BaseTool 默认 GUEST。

napcat 返回的完整成员 list 可能上千条，整张塞进 prompt 会爆——本工具只回
count（总数）+ 前 limit 条精简成员（默认 200，上限 500），每条仅留
user_id/nickname/card/role。

OneBot action：get_group_member_list(group_id)。
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

_USAGE_PROMPT = load_sibling_md(__file__, "get_member_list.md")

# 默认返回条数与硬上限——再大也会被截断，防止撑爆 LLM prompt。
_DEFAULT_LIMIT = 200
_MAX_LIMIT = 500


def _slim_member(member: Any) -> dict:
    """把单条成员精简成 LLM 真正用得上的几个字段，丢弃头像/等级等冗余。"""
    if not isinstance(member, dict):
        return {}
    return {
        "user_id": member.get("user_id"),
        "nickname": member.get("nickname"),
        "card": member.get("card"),
        "role": member.get("role"),
    }


class GetMemberListTool(BaseTool):
    name = "get_member_list"
    allowed_scopes = ("group",)
    # required_permission 用 BaseTool 默认 GUEST（查询无副作用）
    usage_prompt = _USAGE_PROMPT
    description = (
        "List members of the CURRENT group. Operates on the current group "
        "only — group_id comes from your scope, you cannot list another group. "
        "Optionally pass limit (default 200, capped at 500) to cap how many "
        "entries you get back. Returns count (the total member count) and "
        "members (a list TRUNCATED to limit, each with user_id, nickname, "
        "card, role). Read-only; the list is capped to avoid flooding your "
        "context."
    )
    arguments_schema = {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": (
                    "Max members to return (default 200, capped at 500). The "
                    "full total is still reported as count."
                ),
                "default": _DEFAULT_LIMIT,
            },
        },
        "required": [],
    }

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        if fail := await self.enforce_access(context):
            return fail
        group_id, fail = require_group_scope(context, self.name)
        if fail:
            return fail
        limit_raw = arguments.get("limit")
        if limit_raw is None:
            limit = _DEFAULT_LIMIT
        else:
            limit, fail = coerce_int(limit_raw, f"{self.name}.limit")
            if fail:
                return fail
        limit = max(1, min(limit, _MAX_LIMIT))

        bot, fail = get_bot()
        if fail:
            return fail
        raw, fail = await call_action(
            bot, "get_group_member_list", group_id=group_id
        )
        if fail:
            return fail
        members = raw if isinstance(raw, list) else []
        slim = [_slim_member(m) for m in members[:limit]]
        logger.info(
            "[{}] group={} total={} returned={}",
            self.name,
            group_id,
            len(members),
            len(slim),
        )
        return ToolOutcome.success({"count": len(members), "members": slim})

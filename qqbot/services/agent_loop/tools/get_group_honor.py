"""GetGroupHonorTool —— 查询当前群的荣誉榜（龙王 / 群聊之火 / 群聊炽焰 ...）。

仅 GroupAgentLoop 可用（allowed_scopes=("group",)）；group_id 锁定当前群——
从 scope_key 注入、不由 LLM 传（隔离契约 §9：一个群的 agent 不能查别的群）。
napcat 动作失败（查询失败等）由 call_action 折成 upstream_action_failed **返回**；
权限/角色/scope 判定在 execute() 首行 enforce_access（先于任何 napcat 动作）返回
对应失败 outcome。全程无 raise。

权限：查询无副作用，沿用 BaseTool 默认 GUEST。

type 选 talkative/performer/legend/strong_newbie/emotion/all（默认 all）。返回
精简：current_talkative（若有）+ 各榜单各只留前 5 条（user_id/nickname/
description 兜底），防止整张榜单撑爆 prompt。

OneBot action：get_group_honor_info(group_id, type)。
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

_USAGE_PROMPT = load_sibling_md(__file__, "get_group_honor.md")

# 每个榜单只保留前几条，整张榜可能几十上百条，截断防止撑爆 LLM prompt。
_HONOR_LIST_LIMIT = 5


def _slim_honor_entry(entry: Any) -> dict:
    """把单条荣誉成员精简成 user_id/nickname/description，丢弃头像等冗余。"""
    if not isinstance(entry, dict):
        return {}
    return {
        "user_id": entry.get("user_id"),
        "nickname": entry.get("nickname"),
        "description": entry.get("description"),
    }


class GetGroupHonorTool(BaseTool):
    name = "get_group_honor"
    allowed_scopes = ("group",)
    # required_permission 用 BaseTool 默认 GUEST（查询无副作用）
    usage_prompt = _USAGE_PROMPT
    description = (
        "Get honor / leaderboard info for the CURRENT group (talkative streak "
        "龙王, performers, legends, etc.). Operates on the current group only "
        "— group_id comes from your scope, you cannot query another group. "
        "Optionally pass type: one of talkative, performer, legend, "
        "strong_newbie, emotion, or all (default all). Returns "
        "current_talkative (if any) plus each leaderboard list trimmed to its "
        "top 5 entries (user_id, nickname, description). Read-only, no side "
        "effects."
    )
    arguments_schema = {
        "type": "object",
        "properties": {
            "type": {
                "type": "string",
                "description": (
                    "Which honor leaderboard to fetch: talkative, performer, "
                    "legend, strong_newbie, emotion, or all (default all)."
                ),
                "enum": [
                    "talkative",
                    "performer",
                    "legend",
                    "strong_newbie",
                    "emotion",
                    "all",
                ],
                "default": "all",
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
        # 别用 type 作变量名（遮蔽内建），用 honor_type；传参时 type=honor_type。
        honor_type = arguments.get("type") or "all"

        bot, fail = get_bot()
        if fail:
            return fail
        info, fail = await call_action(
            bot, "get_group_honor_info", group_id=group_id, type=honor_type
        )
        if fail:
            return fail
        info = info or {}

        slim: dict[str, Any] = {"group_id": group_id, "type": honor_type}
        current = info.get("current_talkative")
        if current:
            slim["current_talkative"] = _slim_honor_entry(current)
        # 动态收所有 *_list 榜单（talkative_list / performer_list / ...）各取前 5。
        for key, value in info.items():
            if key.endswith("_list") and isinstance(value, list):
                slim[key] = [
                    _slim_honor_entry(e) for e in value[:_HONOR_LIST_LIMIT]
                ]
        logger.info("[{}] group={} type={}", self.name, group_id, honor_type)
        return ToolOutcome.success(slim)

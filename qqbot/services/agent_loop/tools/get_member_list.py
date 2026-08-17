"""GetMemberListTool —— 拉取当前群的成员列表（截断防爆）。

仅 GroupAgentLoop 可用（allowed_scopes=("group",)）；group_id 锁定当前群——
从 scope_key 注入、不由 LLM 传（隔离契约 §9：一个群的 agent 不能查别的群）。
napcat 动作失败（拉取失败等）由 call_action 折成 upstream_action_failed **返回**；
权限/角色/scope 判定在 execute() 首行 enforce_access（先于任何 napcat 动作）返回
对应失败 outcome。全程无 raise。

权限：查询无副作用，沿用 BaseTool 默认 GUEST。

napcat 返回的完整成员 list 可能上千条，整张塞进 prompt 会爆——本工具只回
count（群总人数）+ matched（过滤后条数）+ 前 limit 条精简成员（默认 200，上限
500），每条仅留 user_id/nickname/card/role（正被禁言的额外带 banned_until）。

2026-07-07 重做恢复时的增强：
- ``role`` 可选过滤（owner/admin/member）——"列出所有管理员"这类问题在大群
  不再被截断吃掉目标（过滤在截断**之前**做）；
- ``include_activity`` 可选开关——附带 join_time/last_sent_time（epoch →
  Asia/Shanghai ISO），用于"谁最近活跃/谁在潜水"类问题；默认关省 token；
- ``banned_until``——成员的 shut_up_timestamp 在未来（正被禁言）时附带，
  已过期/未禁言不占键。

OneBot action：get_group_member_list(group_id)。
"""

from __future__ import annotations

from typing import Any

from qqbot.core.logging import get_logger
from qqbot.services.agent_loop.prompts import load_sibling_md
from qqbot.services.agent_loop.tool_registry import BaseTool, ToolOutcome
from qqbot.services.agent_loop.tools._onebot_common import (
    call_action,
    coerce_bool,
    coerce_int,
    epoch_to_iso,
    get_bot,
    require_group_scope,
)

logger = get_logger(__name__)

_USAGE_PROMPT = load_sibling_md(__file__, "get_member_list.md")

# 默认返回条数与硬上限——再大也会被截断，防止撑爆 LLM prompt。
_DEFAULT_LIMIT = 200
_MAX_LIMIT = 500

_VALID_ROLES = ("owner", "admin", "member")


def _slim_member(member: Any, *, include_activity: bool) -> dict:
    """把单条成员精简成 LLM 真正用得上的几个字段，丢弃头像/等级等冗余。"""
    if not isinstance(member, dict):
        return {}
    slim = {
        "user_id": member.get("user_id"),
        "nickname": member.get("nickname"),
        "card": member.get("card"),
        "role": member.get("role"),
    }
    banned_until = epoch_to_iso(
        member.get("shut_up_timestamp"), future_only=True
    )
    if banned_until is not None:
        slim["banned_until"] = banned_until
    if include_activity:
        slim["join_time"] = epoch_to_iso(member.get("join_time"))
        slim["last_sent_time"] = epoch_to_iso(member.get("last_sent_time"))
    return slim


def _member_role(member: Any) -> str | None:
    if not isinstance(member, dict):
        return None
    role = member.get("role")
    return role.strip().lower() if isinstance(role, str) else None


class GetMemberListTool(BaseTool):
    name = "get_member_list"
    program_kind = "effect"
    max_call_sites = 4
    allowed_scopes = ("group",)
    # required_permission 用 BaseTool 默认 GUEST（查询无副作用）
    usage_prompt = _USAGE_PROMPT
    description = (
        "查询当前群的成员列表。group_id 从当前 scope 注入。可通过 role 过滤群"
        "角色，通过 limit 控制返回条数，通过 include_activity 控制是否返回入群与"
        "最后发言时间。结果包含总人数 count、匹配人数 matched 和截断后的 members。"
        "该调用为只读操作。"
    )
    arguments_schema = {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": (
                    "members 的最大返回条数，默认 200，最大 500；count 仍返回完整"
                    "成员总数。"
                ),
                "default": _DEFAULT_LIMIT,
            },
            "role": {
                "type": "string",
                "enum": ["owner", "admin", "member"],
                "description": (
                    "可选的群角色过滤条件；支持 owner、admin、member，过滤先于"
                    "limit 截断执行。"
                ),
            },
            "include_activity": {
                "type": "boolean",
                "default": False,
                "description": (
                    "为 true 时，每个成员条目附带 Asia/Shanghai 时区的 join_time "
                    "和 last_sent_time。"
                ),
            },
        },
        "required": [],
    }
    result_schema = {
        "type": "object",
        "properties": {
            "count": {"type": "integer"},
            "matched": {"type": "integer"},
            "members": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "user_id": {"type": ["integer", "string", "null"]},
                        "nickname": {"type": ["string", "null"]},
                        "card": {"type": ["string", "null"]},
                        "role": {"type": ["string", "null"]},
                        "banned_until": {"type": ["string", "null"]},
                        "join_time": {"type": ["string", "null"]},
                        "last_sent_time": {"type": ["string", "null"]},
                    },
                    "required": ["user_id", "nickname", "card", "role"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["count", "matched", "members"],
        "additionalProperties": False,
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
        role_raw = arguments.get("role")
        if role_raw is None:
            role = None
        else:
            role = str(role_raw).strip().lower()
            if role not in _VALID_ROLES:
                return ToolOutcome.failure(
                    "invalid_arguments",
                    f"{self.name}.role must be one of {list(_VALID_ROLES)}, "
                    f"got {role_raw!r}",
                )
        include_activity, fail = coerce_bool(
            arguments.get("include_activity"),
            f"{self.name}.include_activity",
            default=False,
        )
        if fail:
            return fail

        bot, fail = get_bot()
        if fail:
            return fail
        response, fail = await call_action(
            bot,
            "get_group_member_list",
            effect=False,
            group_id=group_id,
        )
        if fail:
            return fail
        raw = response.data if response is not None else None
        members = raw if isinstance(raw, list) else []
        # role 过滤在截断之前——"列出所有管理员"不能被 limit 吃掉目标。
        filtered = (
            [m for m in members if _member_role(m) == role]
            if role is not None
            else members
        )
        slim = [
            _slim_member(m, include_activity=include_activity)
            for m in filtered[:limit]
        ]
        logger.info(
            "[{}] group={} total={} matched={} returned={}",
            self.name,
            group_id,
            len(members),
            len(filtered),
            len(slim),
        )
        return ToolOutcome.success(
            {"count": len(members), "matched": len(filtered), "members": slim}
        )

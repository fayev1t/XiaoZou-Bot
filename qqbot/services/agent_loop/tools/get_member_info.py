"""GetMemberInfoTool —— 查询当前群里某个成员的资料。

仅 GroupAgentLoop 可用（allowed_scopes=("group",)）；group_id 锁定当前群——
从 scope_key 注入、不由 LLM 传（隔离契约 §9：一个群的 agent 不能查别的群）。
napcat 动作失败（查不到成员等）由 call_action 折成 upstream_action_failed
**返回**；权限/角色/scope 判定在 execute() 首行 enforce_access（先于任何 napcat
动作）返回对应失败 outcome。全程无 raise。

权限：查询无副作用，沿用 BaseTool 默认 GUEST。

返回精简后的成员字段（nickname/card/role/level/title/join_time/...），napcat
原始结果里的冗余字段一律丢弃，避免撑爆 LLM prompt。

2026-07-07 重做恢复时的增强：
- join_time / last_sent_time 从裸 epoch 转 Asia/Shanghai ISO（LLM 对 epoch 的
  心算换算很不可靠），0/缺失 → None；
- 新增 banned_until——shut_up_timestamp 在未来（正被禁言）时给 ISO 到期时间，
  否则 None（键恒在，单人查询保持键稳定）；
- no_cache=True——查单人常发生在权限核对/禁言核对前，实时性优先。

OneBot action：get_group_member_info(group_id, user_id, no_cache)。
"""

from __future__ import annotations

from typing import Any

from qqbot.core.logging import get_logger
from qqbot.services.agent_loop.prompts import load_sibling_md
from qqbot.services.agent_loop.tool_registry import BaseTool, ToolOutcome
from qqbot.services.agent_loop.tools._onebot_common import (
    call_action,
    coerce_int,
    epoch_to_iso,
    get_bot,
    require_group_scope,
)

logger = get_logger(__name__)

_USAGE_PROMPT = load_sibling_md(__file__, "get_member_info.md")


class GetMemberInfoTool(BaseTool):
    name = "get_member_info"
    allowed_scopes = ("group",)
    # required_permission 用 BaseTool 默认 GUEST（查询无副作用）
    usage_prompt = _USAGE_PROMPT
    description = (
        "Look up one member's profile in the CURRENT group. Operates on the "
        "current group only — group_id comes from your scope, you cannot query "
        "another group. Pass user_id (the QQ number — read it from a "
        "<message sender_id=\"USER_ID\"> in the timeline). Returns the "
        "member's nickname, group card, role (owner/admin/member), level, "
        "title, join_time / last_sent_time (Asia/Shanghai ISO) and "
        "banned_until (ISO when the member is currently muted, else null). "
        "Read-only, no side effects."
    )
    arguments_schema = {
        "type": "object",
        "properties": {
            "user_id": {
                "type": "integer",
                "description": "QQ number of the member to look up.",
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
        info, fail = await call_action(
            bot,
            "get_group_member_info",
            group_id=group_id,
            user_id=user_id,
            no_cache=True,
        )
        if fail:
            return fail
        info = info or {}
        logger.info("[{}] group={} user={}", self.name, group_id, user_id)
        return ToolOutcome.success({
            "user_id": info.get("user_id", user_id),
            "nickname": info.get("nickname"),
            "card": info.get("card"),
            "role": info.get("role"),
            "level": info.get("level"),
            "title": info.get("title"),
            "join_time": epoch_to_iso(info.get("join_time")),
            "last_sent_time": epoch_to_iso(info.get("last_sent_time")),
            "banned_until": epoch_to_iso(
                info.get("shut_up_timestamp"), future_only=True
            ),
        })

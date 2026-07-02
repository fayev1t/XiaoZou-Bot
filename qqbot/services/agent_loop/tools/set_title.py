"""SetTitleTool —— 给当前群某个成员设置/清除专属头衔。

仅 GroupAgentLoop 可用（allowed_scopes=("group",)）；group_id 锁定当前群——
从 scope_key 注入、不由 LLM 传（隔离契约 §9：一个群的 agent 不能操作别的群）。
napcat 动作失败（bot 非群主 / 目标不存在等）由 call_action 折成
upstream_action_failed **返回**；权限/角色/scope 判定在 execute() 首行
enforce_access（先于任何 napcat 动作）返回对应失败 outcome。全程无 raise。

权限：required_permission=OWNER —— 专属头衔是群主专属权力，须群主授意。

OneBot action：set_group_special_title(group_id, user_id, special_title)。
注意 napcat 的参数名是 special_title。
"""

from __future__ import annotations

from typing import Any

from qqbot.core.logging import get_logger
from qqbot.core.permissions import PermissionTier
from qqbot.services.agent_loop.prompts import load_sibling_md
from qqbot.services.agent_loop.tool_registry import BaseTool, ToolOutcome
from qqbot.services.agent_loop.tools._onebot_common import (
    call_action,
    coerce_int,
    get_bot,
    require_group_scope,
)

logger = get_logger(__name__)

_USAGE_PROMPT = load_sibling_md(__file__, "set_title.md")


class SetTitleTool(BaseTool):
    name = "set_title"
    allowed_scopes = ("group",)
    required_permission = PermissionTier.OWNER
    required_bot_role = "owner"  # set_group_special_title 是群主专属能力，bot 须是群主
    usage_prompt = _USAGE_PROMPT
    description = (
        "Set or clear the special title (专属头衔) of a member in the CURRENT "
        "group. Operates on the current group only — group_id comes from your "
        "scope, you cannot edit titles in another group. Pass user_id (the QQ "
        "number — read it from a <message sender_id=\"USER_ID\"> in the "
        "timeline) and title (the new special title); pass title=\"\" (empty) "
        "to CLEAR it. This is a sensitive privileged action; requires the bot "
        "ITSELF to be the group OWNER (群主) — if it isn't, the call fails and "
        "you'll see the reason next tick."
    )
    arguments_schema = {
        "type": "object",
        "properties": {
            "user_id": {
                "type": "integer",
                "description": "QQ number of the member whose title to set.",
            },
            "title": {
                "type": "string",
                "description": (
                    "New special title. Empty string clears the title."
                ),
                "default": "",
            },
        },
        "required": ["user_id"],
    }

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        # 全程无 raise：每个 helper 返回失败 outcome，直接 return 上来。
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
        title = str(arguments.get("title") or "")

        bot, fail = get_bot()
        if fail:
            return fail
        _, fail = await call_action(
            bot,
            "set_group_special_title",
            group_id=group_id,
            user_id=user_id,
            special_title=title,
        )
        if fail:
            return fail
        logger.info(
            "[{}] group={} user={} title={!r}",
            self.name,
            group_id,
            user_id,
            title,
        )
        return ToolOutcome.success(
            group_id=group_id,
            user_id=user_id,
            title=title,
        )

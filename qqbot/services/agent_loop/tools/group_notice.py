"""GroupNoticeTool —— 在当前群发布群公告。

仅 GroupAgentLoop 可用（allowed_scopes=("group",)）；group_id 锁定当前群——
从 scope_key 注入、不由 LLM 传（隔离契约 §9：一个群的 agent 不能操作别的群）。
napcat 动作失败（bot 权限不足等）由 call_action 折成 upstream_action_failed
**返回**；权限/角色/scope 判定在 execute() 首行 enforce_access（先于任何 napcat
动作）返回对应失败 outcome。全程无 raise。

权限：required_permission=OWNER —— 群公告是全群高曝光广播，须群主授意。

OneBot action：_send_group_notice(group_id, content, image?)。带下划线前缀的
扩展 action 不在 Bot 上自动生成同名方法，必须用 bot.call_api 调。
"""

from __future__ import annotations

from typing import Any

from qqbot.core.logging import get_logger
from qqbot.core.permissions import PermissionTier
from qqbot.services.agent_loop.prompts import load_sibling_md
from qqbot.services.agent_loop.tool_registry import BaseTool, ToolOutcome
from qqbot.services.agent_loop.tools._onebot_common import (
    call_action,
    get_bot,
    require_group_scope,
)

logger = get_logger(__name__)

_USAGE_PROMPT = load_sibling_md(__file__, "group_notice.md")


class GroupNoticeTool(BaseTool):
    name = "group_notice"
    allowed_scopes = ("group",)
    required_permission = PermissionTier.OWNER
    required_bot_role = "admin"  # _send_group_notice 需要 bot 自己是群管理员
    usage_prompt = _USAGE_PROMPT
    description = (
        "Publish a group notice (群公告) in the CURRENT group. Operates on the "
        "current group only — group_id comes from your scope, you cannot post "
        "to another group. Pass content (the notice body text, required, "
        "non-empty) and optionally image (a URL or file path to attach). This "
        "is a high-visibility broadcast everyone in the group sees; requires "
        "the bot itself to be a group admin/owner — if it isn't, the call "
        "fails and you'll see the reason next tick."
    )
    arguments_schema = {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "Notice body text (required, non-empty).",
            },
            "image": {
                "type": "string",
                "description": "Optional image URL or file path to attach.",
            },
        },
        "required": ["content"],
    }

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        # 全程无 raise：每个 helper 返回失败 outcome，直接 return 上来。
        if fail := await self.enforce_access(context):
            return fail
        group_id, fail = require_group_scope(context, self.name)
        if fail:
            return fail
        content = arguments.get("content")
        if not content or not isinstance(content, str) or not content.strip():
            return ToolOutcome.failure(
                "invalid_arguments", f"{self.name}.content is required"
            )
        image = arguments.get("image")

        bot, fail = get_bot()
        if fail:
            return fail
        params: dict[str, Any] = {"group_id": group_id, "content": content}
        if image:
            params["image"] = image
        _, fail = await call_action(bot, "_send_group_notice", **params)
        if fail:
            return fail
        logger.info("[{}] group={} content={}", self.name, group_id, content)
        return ToolOutcome.success(group_id=group_id)

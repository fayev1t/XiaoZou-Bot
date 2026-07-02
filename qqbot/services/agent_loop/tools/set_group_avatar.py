"""SetGroupAvatarTool —— 设置当前群的群头像。

仅 GroupAgentLoop 可用（allowed_scopes=("group",)）；group_id 锁定当前群——
从 scope_key 注入、不由 LLM 传（隔离契约 §9：一个群的 agent 不能操作别的群）。
napcat 动作失败（bot 权限不足 / 图片非法等）由 call_action 折成
upstream_action_failed **返回**；权限/角色/scope 判定在 execute() 首行
enforce_access（先于任何 napcat 动作）返回对应失败 outcome。全程无 raise。

权限：required_permission=OWNER —— 群头像是全群可见的高曝光改动，须群主授意。

注：bot 目前通常没有现成的图片来源（无法自行生成 url / base64），此工具实用性
有限，但仍保留以便有图片来源时可用。

OneBot action：set_group_portrait(group_id, file)。
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

_USAGE_PROMPT = load_sibling_md(__file__, "set_group_avatar.md")


class SetGroupAvatarTool(BaseTool):
    name = "set_group_avatar"
    allowed_scopes = ("group",)
    required_permission = PermissionTier.OWNER
    required_bot_role = "admin"  # set_group_portrait 需要 bot 自己是群管理员
    usage_prompt = _USAGE_PROMPT
    description = (
        "Set the avatar (portrait) of the CURRENT group. Operates on the "
        "current group only — group_id comes from your scope, you cannot "
        "affect another group. Pass file (required): an image source — a URL, "
        "a local file path, or a base64 string. NOTE: the bot usually has no "
        "ready image source of its own, so this tool is of limited usefulness "
        "in practice but is kept for cases where an image source is available. "
        "This is a high-visibility change everyone in the group sees; requires "
        "the bot itself to be a group admin/owner — if it isn't, the call "
        "fails and you'll see the reason next tick."
    )
    arguments_schema = {
        "type": "object",
        "properties": {
            "file": {
                "type": "string",
                "description": (
                    "Image source: URL, local file path, or base64 string "
                    "(required)."
                ),
            },
        },
        "required": ["file"],
    }

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        # 全程无 raise：每个 helper 返回失败 outcome，直接 return 上来。
        if fail := await self.enforce_access(context):
            return fail
        group_id, fail = require_group_scope(context, self.name)
        if fail:
            return fail
        file = arguments.get("file")
        if not file or not isinstance(file, str) or not file.strip():
            return ToolOutcome.failure(
                "invalid_arguments", f"{self.name}.file is required"
            )

        bot, fail = get_bot()
        if fail:
            return fail
        _, fail = await call_action(
            bot, "set_group_portrait", group_id=group_id, file=file
        )
        if fail:
            return fail
        logger.info("[{}] group={} file={}", self.name, group_id, file)
        return ToolOutcome.success(group_id=group_id)

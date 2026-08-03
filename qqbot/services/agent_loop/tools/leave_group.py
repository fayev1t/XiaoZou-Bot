"""LeaveGroupTool —— 让 bot 因极端定向辱骂退出当前群。

仅 GroupAgentLoop 可用（allowed_scopes=("group",)）；group_id 锁定当前群——
从 scope_key 注入、不由 LLM 传（隔离契约 §9：一个群的 agent 不能操作别的群）。
napcat 动作失败（bot 权限不足等）由 call_action 折成 upstream_action_failed
**返回**；权限/角色/scope 判定在 execute() 首行 enforce_access（先于任何 napcat
动作）返回对应失败 outcome。全程无 raise。

触发语义由 Planner 根据当前 timeline 判断：只有明确指向机器人所扮演角色本人的
极端人格侮辱或恶意辱骂才直接调用。这里不做关键词匹配，也不把辱骂者当成“授权者”，
所以 required_permission=GUEST。误触面的硬收口是：工具无业务参数，永远只调用
``set_group_leave(..., is_dismiss=False)``，没有解散群能力。

⚠️ 高危：执行后 bot 直接退出，在该群立即失效、收不到也发不出任何消息，不可逆。

OneBot action：set_group_leave(group_id, is_dismiss=False)。
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

_USAGE_PROMPT = load_sibling_md(__file__, "leave_group.md")


class LeaveGroupTool(BaseTool):
    name = "leave_group"
    allowed_scopes = ("group",)
    # 这是角色面对极端定向辱骂时的自主安全出口，不是由消息发送者授权的群管
    # 操作；辱骂者通常只是普通成员，若要求 OWNER，正确触发也会被权限门禁拦掉。
    required_permission = PermissionTier.GUEST
    # 自己退群不需要 bot 是管理员 → 不设 required_bot_role（保持 BaseTool 默认 None）。
    usage_prompt = _USAGE_PROMPT
    description = (
        "当群内出现明确指向机器人所扮演角色本人的极端人格侮辱或恶意辱骂时，"
        "使机器人立即退出当前群。只退群，不解散群，也不发送告别消息；一般争执、"
        "粗口、玩笑、对他人的辱骂、转述或单纯要求机器人退群都不构成触发条件。"
        "本工具无参数，成功退出后不可撤销。"
    )
    arguments_schema = {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        # 全程无 raise：每个 helper 返回失败 outcome，直接 return 上来。
        if fail := await self.enforce_access(context):
            return fail
        group_id, fail = require_group_scope(context, self.name)
        if fail:
            return fail
        if arguments:
            extras = sorted(str(key) for key in arguments)
            return ToolOutcome.failure(
                "invalid_arguments",
                f"{self.name} takes no arguments; group dismissal is unsupported",
                reason_code="unexpected_argument",
                fields=extras,
            )

        bot, fail = get_bot()
        if fail:
            return fail
        _, fail = await call_action(
            bot, "set_group_leave", group_id=group_id, is_dismiss=False
        )
        if fail:
            return fail
        logger.info("[{}] group={} left=true", self.name, group_id)
        return ToolOutcome.success(group_id=group_id, left=True, is_dismiss=False)

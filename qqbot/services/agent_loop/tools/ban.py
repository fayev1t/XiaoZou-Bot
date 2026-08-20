"""BanTool —— 对当前群的某个成员禁言 / 解除禁言。

仅 GroupAgentLoop 可用（allowed_scopes=("group",)）；group_id 锁定当前群——
从 scope_key 注入、不由 LLM 传（事件系统设计 §11.2：一个群的 agent 不能操作别的群）。
napcat 动作失败（bot 权限不足 / 目标不存在等）由 call_action 折成
upstream_action_failed **返回**；权限/角色/scope 判定在 execute() 首行
enforce_access（先于任何 napcat 动作）返回对应失败 outcome。全程无 raise。

权限：required_permission=ADMIN —— 禁言成员须群管理员/群主在场授意。

OneBot action：set_group_ban(group_id, user_id, duration)。
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
    enforce_actor_outranks_target,
    fetch_member_role,
    get_bot,
    require_group_scope,
)

logger = get_logger(__name__)

_USAGE_PROMPT = load_sibling_md(__file__, "ban.md")


class BanTool(BaseTool):
    name = "ban"
    allowed_scopes = ("group",)
    required_permission = PermissionTier.ADMIN
    required_bot_role = "admin"  # set_group_ban 需要 bot 自己是群管理员
    usage_prompt = _USAGE_PROMPT
    description = (
        "设置或解除当前群中指定成员的禁言。group_id 从当前 scope 注入，user_id "
        "指定成员，duration 指定禁言秒数；duration=0 表示解除禁言。调用要求发起"
        "用户权限不低于 ADMIN，机器人群角色不低于 admin 且严格高于目标成员。"
    )
    arguments_schema = {
        "type": "object",
        "properties": {
            "user_id": {
                "type": "integer",
                "description": "目标成员的 QQ 号。",
            },
            "duration": {
                "type": "integer",
                "description": (
                    "禁言时长，单位为秒；默认 1800，取值 0 表示解除禁言。"
                ),
                "default": 1800,
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
        duration = int(arguments.get("duration", 1800))

        bot, fail = get_bot()
        if fail:
            return fail
        # 细粒度层级前置判定：禁言不了 ≥ 自己角色的人（管理员禁言不了群主/另一管理员）。
        # bot 角色实时解析（enforce_bot_admin 已查并缓存，这里复用）、目标角色实时查；
        # 查不到不拦，交 napcat 兜底（见 _onebot_common 注释）。
        bot_role = await self._effective_bot_role(context)
        target_role = await fetch_member_role(bot, group_id, user_id)
        if fail := enforce_actor_outranks_target(
            self.name, "mute", bot_role, target_role, user_id
        ):
            return fail
        _, fail = await call_action(
            bot,
            "set_group_ban",
            effect=True,
            group_id=group_id,
            user_id=user_id,
            duration=duration,
        )
        if fail:
            return fail
        logger.info(
            "[{}] group={} user={} duration={}",
            self.name,
            group_id,
            user_id,
            duration,
        )
        return ToolOutcome.success(
            group_id=group_id,
            user_id=user_id,
            duration=duration,
        )

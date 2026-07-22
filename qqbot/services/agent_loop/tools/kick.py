"""KickTool — 把某个成员踢出当前群。

仅 GroupAgentLoop 可用（allowed_scopes=("group",)）；group_id 锁定当前群——
从 scope_key 注入、不由 LLM 传（隔离契约 §9：一个群的 agent 不能操作别的群）。

权限/判定全在工具内（execute() 第一行 ``await self.enforce_access``，AgentLoop 不再
闸门）：发起人 tier（required_permission=ADMIN——踢人须群管理员/群主授意，**实时**查
其当前群角色）+ bot 自身角色（required_bot_role="admin"，set_group_kick 需 bot 是群
管理员）。任一不够即**返回** permission_denied_* / tool_unavailable_in_scope，**不发起
任何 napcat 动作**。napcat 动作失败（目标不存在等）由 call_action 折成
upstream_action_failed 返回。全程无 raise。

通用门禁之上还有两道**参数相关**的前置判定（都先于 napcat 动作）：
- 层级：bot 角色须**严格高于**目标（enforce_actor_outranks_target，目标角色实时
  查；查不到**不拦**、交 napcat 兜底——避免把"已退群/查询失败"误判成越权）；
- 自踢防护：user_id 是 bot 自己 → invalid_arguments。set_group_kick 对自身行为
  未定义（部分实现等价退群），"让 bot 退群"是另一个高危操作，不该由踢人误触发。

OneBot action：set_group_kick(group_id, user_id, reject_add_request)。
实测（2026-07-22，NapCat 4.18.8，api_lab）：成功返回 data=null——忽略返回值即
正确；~1s 后 napcat 推 notice.group_decrease（sub_type="kick"、operator_id=bot
自身、user_id=被踢者），经 GroupDecreaseMapper 入库、投影渲染 <notice> 行，
下一拍模型可见"人已移除"的权威确认。get_group_member_info(no_cache) 返回含
role 的 dict（fetch_member_role 前提成立）。暂未实测：失败形态 retcode/wording
目录、reject_add_request 实效、kick_me（api_lab 可随时补测）。
"""

from __future__ import annotations

from typing import Any

from qqbot.core.logging import get_logger
from qqbot.core.permissions import PermissionTier
from qqbot.services.agent_loop.prompts import load_sibling_md
from qqbot.services.agent_loop.tool_registry import BaseTool, ToolOutcome
from qqbot.services.agent_loop.tools._onebot_common import (
    call_action,
    coerce_bool,
    coerce_int,
    enforce_actor_outranks_target,
    fetch_member_role,
    get_bot,
    require_group_scope,
)

logger = get_logger(__name__)

_USAGE_PROMPT = load_sibling_md(__file__, "kick.md")


class KickTool(BaseTool):
    name = "kick"
    allowed_scopes = ("group",)
    required_permission = PermissionTier.ADMIN
    required_bot_role = "admin"  # set_group_kick 需要 bot 自己是群管理员
    usage_prompt = _USAGE_PROMPT
    description = (
        "Remove (kick) a member from the CURRENT group. Operates on the "
        "current group only — group_id comes from your scope, you cannot kick "
        "from another group. Pass user_id (the QQ number to kick — read it "
        "from a <message sender_qq=\"USER_QQ\"> in the timeline) and "
        "optionally reject_add_request=true to also block their future join "
        "requests. This is an ADMIN-level action: only act on an explicit "
        "instruction from a group admin/owner, and set triggered_by_event_id "
        "to that person's message. The bot itself must be a group admin and "
        "strictly outrank the target; it cannot kick itself."
    )
    arguments_schema = {
        "type": "object",
        "properties": {
            "user_id": {
                "type": "integer",
                "description": "QQ number of the member to kick.",
            },
            "reject_add_request": {
                "type": "boolean",
                "description": (
                    "If true, also reject this user's future join requests."
                ),
                "default": False,
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
        reject, fail = coerce_bool(
            arguments.get("reject_add_request"),
            f"{self.name}.reject_add_request",
            default=False,
        )
        if fail:
            return fail

        bot, fail = get_bot()
        if fail:
            return fail
        # 自踢防护：LLM 可能把"让 bot 退群"误表达成踢 bot 自己。set_group_kick
        # 对自身行为未定义（部分实现等价退群），前置拦成确定性 invalid_arguments，
        # 不打 napcat。self_id 缺失（异常 stub）时 str 比较恒不等 → 不拦。
        if str(user_id) == str(getattr(bot, "self_id", "")):
            return ToolOutcome.failure(
                "invalid_arguments",
                f"{self.name} cannot target the bot itself "
                f"(user_id={user_id} is this bot's own account)",
            )
        # 细粒度层级前置判定：踢不掉 ≥ 自己角色的人（admin 踢不掉群主/另一 admin）。
        # bot 角色实时解析（enforce_bot_admin 已查并缓存，这里复用）、目标角色实时查；
        # 查不到不拦，交 napcat 兜底（见 _onebot_common 注释）。
        bot_role = await self._effective_bot_role(context)
        target_role = await fetch_member_role(bot, group_id, user_id)
        if fail := enforce_actor_outranks_target(
            self.name, "kick", bot_role, target_role, user_id
        ):
            return fail
        _, fail = await call_action(
            bot,
            "set_group_kick",
            group_id=group_id,
            user_id=user_id,
            reject_add_request=reject,
        )
        if fail:
            return fail
        logger.info(
            "[{}] group={} user={} reject_add_request={}",
            self.name,
            group_id,
            user_id,
            reject,
        )
        # 结果回显 reject_add_request：LLM 下一 tick 能确认"有没有顺带拒后续申请"，
        # 不必回忆自己传了什么参数（与 respond_to_group_join_request 的回显同风格）。
        return ToolOutcome.success(
            group_id=group_id,
            user_id=user_id,
            reject_add_request=reject,
            applied=True,
        )

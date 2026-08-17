"""GetGroupInfoTool —— 查询当前群的基本信息。

仅 GroupAgentLoop 可用（allowed_scopes=("group",)）；group_id 锁定当前群——
从 scope_key 注入、不由 LLM 传（隔离契约 §9：一个群的 agent 不能查别的群）。
无参数。napcat 动作失败（查询失败等）由 call_action 折成 upstream_action_failed
**返回**；权限/角色/scope 判定在 execute() 首行 enforce_access（先于任何 napcat
动作）返回对应失败 outcome。全程无 raise。

权限：查询无副作用，沿用 BaseTool 默认 GUEST。

返回精简后的 group_id/group_name/member_count/max_member_count；平台给了才透传
的可选字段：group_remark（群备注，group_remark/group_memo 两个候选键）、
group_create_time（建群时间，epoch → Asia/Shanghai ISO）。NapCat 各版本对这两个
字段的返回差异大（老版本常给 0/空），缺失/空值不占键——LLM 见键即可信。

no_cache=True：这个工具的意义就是"群**现在**多少人"，调用频率低，实时性优先
（2026-07-07 重做恢复时从 False 改过来）。

OneBot action：get_group_info(group_id, no_cache)。
"""

from __future__ import annotations

from typing import Any

from qqbot.core.logging import get_logger
from qqbot.services.agent_loop.prompts import load_sibling_md
from qqbot.services.agent_loop.tool_registry import BaseTool, ToolOutcome
from qqbot.services.agent_loop.tools._onebot_common import (
    call_action,
    epoch_to_iso,
    get_bot,
    require_group_scope,
)

logger = get_logger(__name__)

_USAGE_PROMPT = load_sibling_md(__file__, "get_group_info.md")


class GetGroupInfoTool(BaseTool):
    name = "get_group_info"
    program_kind = "effect"
    max_call_sites = 4
    allowed_scopes = ("group",)
    # required_permission 用 BaseTool 默认 GUEST（查询无副作用）
    usage_prompt = _USAGE_PROMPT
    description = (
        "查询当前群的基础资料。无需参数，group_id 从当前 scope 注入。返回 "
        "group_id、group_name、member_count、max_member_count，以及平台提供时的 "
        "group_remark 和 group_create_time。该调用为只读操作。"
    )
    arguments_schema = {
        "type": "object",
        "properties": {},
        "required": [],
    }
    result_schema = {
        "type": "object",
        "properties": {
            "group_id": {"type": ["integer", "string"]},
            "group_name": {"type": ["string", "null"]},
            "member_count": {"type": ["integer", "null"]},
            "max_member_count": {"type": ["integer", "null"]},
            "group_remark": {"type": ["string", "null"]},
            "group_create_time": {"type": ["string", "null"]},
        },
        "required": [
            "group_id",
            "group_name",
            "member_count",
            "max_member_count",
        ],
        "additionalProperties": False,
    }

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        if fail := await self.enforce_access(context):
            return fail
        group_id, fail = require_group_scope(context, self.name)
        if fail:
            return fail

        bot, fail = get_bot()
        if fail:
            return fail
        response, fail = await call_action(
            bot,
            "get_group_info",
            effect=False,
            group_id=group_id,
            no_cache=True,
        )
        if fail:
            return fail
        info = response.data if response is not None else None
        info = info or {}
        result = {
            "group_id": info.get("group_id", group_id),
            "group_name": info.get("group_name"),
            "member_count": info.get("member_count"),
            "max_member_count": info.get("max_member_count"),
        }
        # 平台相关的可选字段：有值才透传，缺失/空值不占键。
        remark = info.get("group_remark") or info.get("group_memo")
        if isinstance(remark, str) and remark.strip():
            result["group_remark"] = remark.strip()
        created = epoch_to_iso(info.get("group_create_time"))
        if created is not None:
            result["group_create_time"] = created
        logger.info("[{}] group={}", self.name, group_id)
        return ToolOutcome.success(result)

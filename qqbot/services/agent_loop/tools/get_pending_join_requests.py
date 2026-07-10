"""GetPendingJoinRequestsTool —— 查询当前群待处理的入群申请（纯 napcat 查询）。

仅 GroupAgentLoop 可用（allowed_scopes=("group",)）；group_id 锁定当前群——
从 scope_key 注入、不由 LLM 传（隔离契约 §9：一个群的 agent 不能查别的群）。
napcat 动作失败由 call_action 折成 upstream_action_failed **返回**；权限/角色/
scope 判定在 execute() 首行 enforce_access（先于任何 napcat 动作）返回对应失败
outcome。全程无 raise。

**纯 API 查询，不回查 agent_events**（2026-07-07 用户决策）：结果只反映 napcat
`get_group_system_msg` 的当前视图，不与 timeline 的 <request> 事件做对齐/补写。
审批仍走 respond_to_group_join_request（按 timeline 行的 event_id）；本工具查到
但 timeline 里没有的申请（bot 离线期间到达，napcat 不补推 request 事件）无法经
工具回执，只能提醒管理员去 QQ 客户端手动处理——usage md 有明确指引。

权限两档：
- required_permission 沿用 BaseTool 默认 GUEST——只读无副作用，且申请信息本就
  渲染进群 timeline，不构成新的信息暴露；
- required_bot_role="admin"——bot 非群管理员时 napcat 侧根本没有本群的申请数据，
  前置拦成明确的 permission_denied_bot_role，好过返回空列表误导 LLM"没有申请"。

接口是**账号全局**的（返回 bot 名下所有群的申请 + bot 自己收到的入群邀请）：
本工具按当前群过滤 join 类条目；invited 类（邀请 bot 入群，system scope 的事，
由 plugin 层 request_auto_approval 处理）一律丢弃、不出现在返回值。归属群号解析
不出的条目同样丢弃——宁可少报，不可跨群泄漏。

响应解析是**防御性**的：get_group_system_msg 是 go-cqhttp 系扩展动作，NapCat
的返回字段名与 go-cqhttp 文档存在已知出入（如 InvitedRequest 驼峰），列表键与
条目字段都按候选集取第一个命中；整体结构认不出 → upstream_payload_invalid
（extra 带收到的顶层键名，便于对着真实 NapCat 版本排查）。⚠️ 候选集以服务器
实测为准，探针结果回来后如有新变体在 _JOIN_LIST_KEYS / _FIELD_* 里补。

**flag / request_id 绝不进返回值**——审批凭证不经 LLM 的既有安全设计
（respond_to_group_join_request 同约定）。

OneBot action：get_group_system_msg（go-cqhttp 扩展，NapCat 实现）。
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

_USAGE_PROMPT = load_sibling_md(__file__, "get_pending_join_requests.md")

# 顶层列表键候选：go-cqhttp 文档用 snake_case，NapCat 有驼峰变体前科。
_JOIN_LIST_KEYS = ("join_requests", "JoinRequest", "JoinRequests", "join_request")
_INVITED_LIST_KEYS = ("invited_requests", "InvitedRequest", "InvitedRequests")

# 条目字段候选（按序取第一个非 None 命中）。
_FIELD_USER_ID = ("requester_uin", "user_id", "requester_id")
_FIELD_NICKNAME = ("requester_nick", "nickname", "requester_nickname")
_FIELD_COMMENT = ("message", "comment")

# 返回条目硬上限——正常积压远到不了；防协议端异常大列表撑爆 prompt。
_MAX_RETURNED = 50

_TRUE_STRINGS = frozenset({"true", "1", "yes"})
_FALSE_STRINGS = frozenset({"false", "0", "no", ""})


class GetPendingJoinRequestsTool(BaseTool):
    name = "get_pending_join_requests"
    allowed_scopes = ("group",)
    # required_permission 用 BaseTool 默认 GUEST（查询无副作用，信息本就进 timeline）
    required_bot_role = "admin"  # 非管理员 bot 收不到入群申请，前置拦成明确失败
    usage_prompt = _USAGE_PROMPT
    description = (
        "Count and list the join requests currently PENDING for the CURRENT "
        "group. Read-only, takes no arguments — group_id comes from your "
        "scope. Returns pending_count plus per-request user_id / nickname / "
        "comment (never the napcat flag) and handled_recent_count. To approve "
        "or reject one, match the applicant to a <request kind=\"group.add\"> "
        "row in the timeline by user_id and call "
        "respond_to_group_join_request; a pending request with no timeline "
        "row (arrived while the bot was offline) must be handled by an admin "
        "in the QQ client. The bot itself must be a group admin."
    )
    arguments_schema = {
        "type": "object",
        "properties": {},
        "required": [],
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
        raw, fail = await call_action(bot, "get_group_system_msg")
        if fail:
            return fail
        join_items, fail = _extract_join_list(raw)
        if fail:
            return fail

        pending: list[dict] = []
        handled = 0
        for item in join_items:
            if not isinstance(item, dict):
                continue
            item_group = _coerce_optional_int(item.get("group_id"))
            if item_group != group_id:
                # 别的群、或归属解析不出的条目一律不出本群 scope（隔离 §9）。
                continue
            if _coerce_checked(item.get("checked")):
                handled += 1
                continue
            pending.append(
                {
                    "user_id": _coerce_optional_int(
                        _first_present(item, _FIELD_USER_ID)
                    ),
                    "nickname": _coerce_optional_str(
                        _first_present(item, _FIELD_NICKNAME)
                    ),
                    "comment": _coerce_optional_str(
                        _first_present(item, _FIELD_COMMENT)
                    ),
                }
            )

        logger.info(
            "[{}] group={} pending={} handled={}",
            self.name,
            group_id,
            len(pending),
            handled,
        )
        return ToolOutcome.success(
            {
                "group_id": group_id,
                "pending_count": len(pending),
                "requests": pending[:_MAX_RETURNED],
                "handled_recent_count": handled,
                # 协议端只回最近若干条系统消息，积压大时计数可能偏低——恒 true，
                # 让 LLM 表述"至少 N 个"而不是咬死精确值。
                "may_be_incomplete": True,
            }
        )


def _extract_join_list(raw: Any) -> tuple[list | None, ToolOutcome | None]:
    """从 get_group_system_msg 响应里取入群申请列表。

    成功 → ``(list, None)``（键存在但值为 None 视作空列表——没有申请时 NapCat
    可能回 null）；结构认不出 → ``(None, upstream_payload_invalid)``，extra 带
    收到的顶层键名供探针排查。只有 invited 键、没有 join 键 → 空列表（本工具
    不管邀请类）。
    """
    if not isinstance(raw, dict):
        return None, ToolOutcome.failure(
            "upstream_payload_invalid",
            "get_group_system_msg returned "
            f"{type(raw).__name__}, expected an object",
            action="get_group_system_msg",
        )
    join_key = next((k for k in _JOIN_LIST_KEYS if k in raw), None)
    invited_key = next((k for k in _INVITED_LIST_KEYS if k in raw), None)
    if join_key is None and invited_key is None:
        keys = sorted(str(k) for k in raw)
        return None, ToolOutcome.failure(
            "upstream_payload_invalid",
            "get_group_system_msg response has no recognizable request "
            f"list; top-level keys: {keys}",
            action="get_group_system_msg",
            received_keys=keys,
        )
    value = raw.get(join_key) if join_key is not None else None
    if value is None:
        return [], None
    if not isinstance(value, list):
        return None, ToolOutcome.failure(
            "upstream_payload_invalid",
            f"get_group_system_msg {join_key} is "
            f"{type(value).__name__}, expected a list",
            action="get_group_system_msg",
        )
    return value, None


def _first_present(item: dict, keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = item.get(key)
        if value is not None:
            return value
    return None


def _coerce_checked(value: Any) -> bool:
    """checked 字段的宽松 bool 化：bool 原样；数值非零为真；字符串按词表。
    缺失 / None / 认不出 → False（当作**待处理**——宁可多报一条给管理员核对，
    也不吞掉一条真实待处理的申请）。不复用 coerce_bool：这里要的是"不认识就
    默认 False"的兜底语义，不是 invalid_arguments 失败。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        v = value.strip().lower()
        if v in _TRUE_STRINGS:
            return True
        if v in _FALSE_STRINGS:
            return False
    return False


def _coerce_optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None

"""RespondToGroupJoinRequestTool —— 同意 / 拒绝**本群**的入群申请。

拆分自已删除的 respond_to_request（原统一处理好友/加群/邀请、仅 system scope）：
好友申请与邀请入群改由 plugin 层 request_auto_approval 自动同意；入群申请
（``external.request.group.add``）进目标群 timeline，由群内 LLM 在管理员明确
授权后调本工具回执 napcat。

仅 GroupAgentLoop 可用（``allowed_scopes=("group",)``）。两道闸门与 ban/kick
同构（enforce_access 在 execute() 首行）：

- ``required_permission=ADMIN`` —— 放人进群/拒之门外须群管理员/群主授意。LLM
  回填 ``triggered_by_event_id`` 指向授权者那条消息，闸门据此**实时**向 napcat
  查其当前群角色——权限不由 LLM 自报，指认错人/无人授权都会被拒。
- ``required_bot_role="admin"`` —— set_group_add_request 需要 bot 自己是群管理员。

LLM 只回填 ``request_event_id``（``<request event_id="...">`` 给出）+ ``approve``。
napcat 处理申请用的 ``flag`` 凭证由本工具用 request_event_id 反查事件 payload
得到，**不经 LLM 复述**——避免长串 flag 照抄出错，也顺带校验事件真实存在。

反查后加两道校验（原工具没有、群 scope 下必需）：
- type 必须**恰为** ``external.request.group.add``（好友/邀请事件的 event_id
  调不动本工具——那两类根本不该由群内处理）；
- 事件的 group_id 必须等于当前 scope 的群号——event_id 全局唯一，不锁群的话
  A 群管理员的授权就能批到 B 群的申请。

OneBot action：set_group_add_request(flag, sub_type="add", approve, reason)。

契约：任务与决策契约.md §2.2；事件系统设计.md §4.1、§10.2。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from qqbot.core.logging import get_logger
from qqbot.core.permissions import PermissionTier
from qqbot.models.agent_event import AgentEvent
from qqbot.services.agent_loop.projection import (
    _EventSnapshot,
    _snapshot_from_row,
)
from qqbot.services.agent_loop.prompts import load_sibling_md
from qqbot.services.agent_loop.tool_registry import BaseTool, ToolOutcome
from qqbot.services.agent_loop.tools._onebot_common import (
    call_action,
    coerce_bool,
    get_bot,
    require_group_scope,
)

logger = get_logger(__name__)

_USAGE_PROMPT = load_sibling_md(__file__, "respond_to_group_join_request.md")

_REQUEST_TYPE = "external.request.group.add"


class RespondToGroupJoinRequestTool(BaseTool):
    """实现 Tool 协议。无构造依赖：session_factory（反查 request 事件）从
    run() 的 context 进，由 ToolWorker 统一注入；Bot 实例从 bot_registry 取。
    """

    name = "respond_to_group_join_request"
    allowed_scopes = ("group",)
    required_permission = PermissionTier.ADMIN
    required_bot_role = "admin"  # set_group_add_request 需要 bot 自己是群管理员
    usage_prompt = _USAGE_PROMPT
    description = (
        "Approve or reject a pending join request to the CURRENT group (a "
        "<request kind=\"group.add\"/> row in the timeline). Pass "
        "request_event_id copied verbatim from that row's event_id attribute, "
        "plus approve=true (let them in) or approve=false (turn them away); "
        "an optional reason is shown to the applicant when rejecting. This is "
        "an ADMIN-level action: only act on an explicit, unambiguous "
        "instruction from a group admin/owner, and set triggered_by_event_id "
        "to that person's message. Never decide on your own, and handle each "
        "request only once."
    )
    arguments_schema = {
        "type": "object",
        "properties": {
            "request_event_id": {
                "type": "string",
                "description": (
                    "The event_id of the <request kind=\"group.add\"> row to "
                    "act on (copy it verbatim from the timeline). The tool "
                    "looks up the napcat flag from this event itself — you "
                    "never handle the flag."
                ),
            },
            "approve": {
                "type": "boolean",
                "description": "true = accept the applicant; false = reject.",
            },
            "reason": {
                "type": "string",
                "description": (
                    "Optional. Rejection reason shown to the applicant "
                    "(ignored when approving)."
                ),
            },
        },
        "required": ["request_event_id", "approve"],
    }

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        # enforce_access = scope（仅 group）+ 发起人 tier（ADMIN，实时查授权者
        # 群角色）+ bot 自身角色（admin，实时查）。全程无 raise：失败一律
        # return failure outcome。
        if fail := await self.enforce_access(context):
            return fail
        group_id, fail = require_group_scope(context, self.name)
        if fail:
            return fail

        session_factory = context.get("session_factory")
        if session_factory is None:
            return ToolOutcome.failure(
                "invalid_arguments",
                f"{self.name} requires session_factory from caller context",
            )
        self._session_factory = session_factory

        request_event_id = _coerce_str(arguments.get("request_event_id"))
        if not request_event_id:
            return ToolOutcome.failure(
                "invalid_arguments",
                f"{self.name}.request_event_id is required",
            )
        # approve 必填 bool；coerce_bool 稳妥转（"false"/"0"/"no" → False），
        # 防止裸 bool("false") 误判成同意。
        approve, fail = coerce_bool(
            arguments.get("approve"), f"{self.name}.approve"
        )
        if fail:
            return fail
        reason = _coerce_str(arguments.get("reason")) or ""

        snap = await self._load_request(request_event_id)
        if snap is None:
            return ToolOutcome.failure(
                "invalid_arguments",
                f"no event found with event_id={request_event_id!r}",
            )
        if snap.type != _REQUEST_TYPE:
            return ToolOutcome.failure(
                "invalid_arguments",
                f"event {request_event_id!r} is not a group join request "
                f"(type={snap.type!r}); this tool only handles "
                f"{_REQUEST_TYPE}",
            )
        # 锁群：事件所属群（列优先，老数据退 payload）必须是当前群。
        event_group = snap.group_id
        if event_group is None:
            event_group = _coerce_int(snap.payload.get("group_id"))
        if event_group != group_id:
            return ToolOutcome.failure(
                "invalid_arguments",
                f"request {request_event_id!r} belongs to group "
                f"{event_group!r}, not the current group {group_id}; "
                "you can only respond to join requests of this group",
            )

        flag = _coerce_str(snap.payload.get("flag"))
        if not flag:
            return ToolOutcome.failure(
                "invalid_arguments",
                f"request event {request_event_id!r} has no flag in payload; "
                "cannot respond",
            )

        bot, fail = get_bot()
        if fail:
            return fail
        _, fail = await call_action(
            bot,
            "set_group_add_request",
            flag=flag,
            sub_type="add",
            approve=approve,
            reason=reason,
        )
        if fail:
            return fail

        logger.info(
            "[{}] group={} event={} user={} approve={}",
            self.name,
            group_id,
            request_event_id,
            snap.user_id,
            approve,
        )
        return ToolOutcome.success(
            {
                "request_event_id": request_event_id,
                "group_id": group_id,
                "user_id": snap.user_id,
                "approve": approve,
                "applied": True,
            }
        )

    async def _load_request(self, event_id: str) -> _EventSnapshot | None:
        """按 event_id 查回那条申请事件，取 flag / group_id / user_id。

        独立成方法便于单测 stub，避免打真 DB（与 search_history._query 同模式）。
        """
        stmt = (
            select(AgentEvent)
            .where(AgentEvent.event_id == event_id)
            .limit(1)
        )
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            row = result.scalars().first()
        if row is None:
            return None
        return _snapshot_from_row(row)


def _coerce_str(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _coerce_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

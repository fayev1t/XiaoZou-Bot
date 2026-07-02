"""RespondToRequestTool — 同意 / 拒绝好友申请、加群申请、入群邀请。

仅 SystemAgentLoop 可用（``allowed_scopes=("system",)``）：好友 / 加群申请经
EventIngest 映射成 ``scope=system`` 的 ``external.request.*`` 事件、``wake_system``
唤醒 SystemAgentLoop（事件系统设计.md §4.1、§10.2）。LLM 在 timeline 的
``<request .../>`` 行看到申请后调本工具回执 napcat。

LLM 只需回填 ``request_event_id``（``<request event_id="...">`` 给出）+
``approve``（同意 / 拒绝）。napcat 处理申请用的 ``flag`` 凭证由本工具用
``request_event_id`` 反查事件 payload 得到，**不经 LLM 复述**——避免长串
flag 照抄出错，也顺带校验"这条 request 确实存在且是 request 事件"。

权限：``required_permission=GUEST`` / ``required_bot_role=None``（都走 BaseTool
默认）。这是 bot 的自主行政决策：system scope 下没有"触发用户"（申请人不是
授权者，其 tier 恒 GUEST），``bot_role`` 也恒 None——所以契约里"预计 ≥ADMIN /
required_bot_role"在此 scope 会让闸门**永远拒绝**。访问控制改由
``allowed_scopes``（只有 SystemAgentLoop 能调）保证；加群审批是否真有权（bot
得是目标群管理员）由 napcat 调用时校验，失败原样返回给 LLM 下一轮处理。

调 OneBot V11 API（经 get_bot() 拿 Bot 实例，与其它 napcat 工具同路）：
  friend → set_friend_add_request(flag, approve, remark)
  group  → set_group_add_request(flag, sub_type, approve, reason)

契约：任务与决策契约.md §2.2（respond_to_request 仅 system scope）；
事件系统设计.md §10.2（SystemAgentLoop 决策同意 / 拒绝 / 忽略）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from qqbot.core.logging import get_logger
from qqbot.models.agent_event import AgentEvent
from qqbot.services.agent_loop.projection import (
    _EventSnapshot,
    _snapshot_from_row,
)
from qqbot.services.agent_loop.tool_registry import BaseTool, ToolOutcome
from qqbot.services.agent_loop.tools._onebot_common import (
    call_action,
    coerce_bool,
    get_bot,
)

logger = get_logger(__name__)


class RespondToRequestTool(BaseTool):
    """实现 Tool 协议。无构造依赖：session_factory（反查 request 事件）从
    run() 的 context 进，由 ToolWorker 统一注入；Bot 实例从 bot_registry 取
    —— 与 websearch（同步调外部）/ send_message（经 bot_registry 出货）同构。
    """

    name = "respond_to_request"
    # 仅 SystemAgentLoop 可见可调；GroupAgentLoop 的 catalog 里不出现它。
    allowed_scopes = ("system",)
    description = (
        "Approve or reject an incoming friend request / group-join request / "
        "group invitation. Only available in the system scope, where such "
        "requests arrive as <request .../> rows in the timeline. Pass "
        "request_event_id copied verbatim from that row's event_id attribute, "
        "plus approve=true (accept) or approve=false (reject). Optionally give "
        "a reason (shown to the applicant when rejecting a GROUP request) or a "
        "remark (friend note set when approving a FRIEND request). You decide "
        "autonomously whether to let someone in — no user is authorising you. "
        "Handle each request only once: after a successful tool-call for it, "
        "do not call again."
    )
    # required_permission / required_bot_role 用 BaseTool 默认（GUEST / 不限）。
    # 理由见模块 docstring：system scope 无触发用户、bot_role 恒 None，提权门
    # 在此 scope 只会让工具永远失败；访问控制靠 allowed_scopes 实现。
    arguments_schema = {
        "type": "object",
        "properties": {
            "request_event_id": {
                "type": "string",
                "description": (
                    "The event_id of the <request> row to act on (copy it "
                    "verbatim from the timeline). The tool looks up the napcat "
                    "flag from this event itself — you never handle the flag."
                ),
            },
            "approve": {
                "type": "boolean",
                "description": "true = accept the request; false = reject it.",
            },
            "reason": {
                "type": "string",
                "description": (
                    "Optional. Rejection reason for a GROUP request "
                    "(set_group_add_request reason). Ignored for friend "
                    "requests and when approving."
                ),
            },
            "remark": {
                "type": "string",
                "description": (
                    "Optional. Friend remark/note to set when APPROVING a "
                    "friend request. Ignored for group requests."
                ),
            },
        },
        "required": ["request_event_id", "approve"],
    }

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        # scope 闸门下放工具：enforce_access → enforce_scope 用 allowed_scopes=
        # ("system",) 拦下任何非 system 调用（tool_unavailable_in_scope）。
        # GUEST + 无 bot 角色要求，enforce_permission / enforce_bot_admin 实为 no-op。
        # 全程无 raise：失败一律 return failure outcome。
        if fail := await self.enforce_access(context):
            return fail

        session_factory = context.get("session_factory")
        if session_factory is None:
            return ToolOutcome.failure(
                "invalid_arguments",
                "respond_to_request requires session_factory from caller context",
            )
        self._session_factory = session_factory

        request_event_id = _coerce_str(arguments.get("request_event_id"))
        if not request_event_id:
            return ToolOutcome.failure(
                "invalid_arguments",
                "respond_to_request.request_event_id is required",
            )
        # approve 是必填 bool：缺键 / 非法值都报错，显式 False 合法（拒绝）。
        # 关键：裸 bool("false") 会误判成 True → 同意本该拒绝的申请，故用
        # coerce_bool 稳妥转（"false"/"0"/"no" → False）。
        approve, fail = coerce_bool(
            arguments.get("approve"), "respond_to_request.approve"
        )
        if fail:
            return fail
        reason = _coerce_str(arguments.get("reason")) or ""
        remark = _coerce_str(arguments.get("remark")) or ""

        snap = await self._load_request(request_event_id)
        if snap is None:
            return ToolOutcome.failure(
                "invalid_arguments",
                f"no event found with event_id={request_event_id!r}",
            )
        if not snap.type.startswith("external.request."):
            return ToolOutcome.failure(
                "invalid_arguments",
                f"event {request_event_id!r} is not a request event "
                f"(type={snap.type!r})",
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

        # type 决定调哪个 OneBot API；friend 没有 sub_type，group 的
        # sub_type（add / invite）由 GroupRequestMapper 写在 payload 里。
        request_type = (
            "friend" if snap.type == "external.request.friend" else "group"
        )
        if request_type == "friend":
            _, fail = await call_action(
                bot,
                "set_friend_add_request",
                flag=flag,
                approve=approve,
                remark=remark,
            )
        else:
            sub_type = _coerce_str(snap.payload.get("sub_type")) or "add"
            _, fail = await call_action(
                bot,
                "set_group_add_request",
                flag=flag,
                sub_type=sub_type,
                approve=approve,
                reason=reason,
            )
        if fail:
            return fail

        logger.info(
            "[respond_to_request] {} event={} approve={}",
            request_type,
            request_event_id,
            approve,
        )
        return ToolOutcome.success(
            {
                "request_event_id": request_event_id,
                "request_type": request_type,
                "approve": approve,
                "applied": True,
            }
        )

    async def _load_request(self, event_id: str) -> _EventSnapshot | None:
        """按 event_id 查回那条 ``external.request.*`` 事件，取 flag / sub_type。

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

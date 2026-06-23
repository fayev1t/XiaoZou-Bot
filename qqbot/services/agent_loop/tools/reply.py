"""ReplyTool — 把"发言"从 first-class Action 降级为普通工具。

设计动机：群聊里大多数消息并非对小奏说，把 reply 暴露为工具能让 LLM 每
tick 自然做"要不要说话"的决定——和调 websearch / search_history 一样
opt-in，而不是被 ReplyAction 的存在诱导成"必须选 idle 或 reply"的二
分法。

行为：
- run() 校验 arguments.target.kind / group_id|user_id 与当前 scope_key 是否匹配；
  不匹配 raise ValueError → ToolWorker 写 agent.tool_failed
- 通过 EventWriter 写一条 agent.reply_emitted（payload 同旧 ReplyAction 路径，
  ReplySendWorker 完全无感知 —— 它只查 agent.reply_emitted 这张表）
- 立即调用注入的 wake_reply_worker 回调唤醒 ReplySendWorker；回调为 None
  时退化为不主动唤醒（worker 启动期的 catchup 仍能兜底，仅延迟变长）
- 返回 {"reply_event_id": "...", "queued": true} 作为 tool_result

旧 ReplyAction / `agent.reply_emitted` 事件类型 / ReplySendWorker 全部保留；
本工具只是接管"上游写 reply_emitted"这一职责。

契约：任务与决策契约.md §6 (reply 链路保持 reply_emitted → delivered/failed
不变；仅入口从 ReplyAction 改为 ReplyTool)
"""

from __future__ import annotations

from typing import Any, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from qqbot.core.ids import new_event_id
from qqbot.core.logging import get_logger
from qqbot.core.permissions import PermissionTier
from qqbot.services.agent_loop.event_writer import (
    parse_scope_key,
    write_agent_event,
)
from qqbot.services.agent_loop.prompts import load_sibling_md

logger = get_logger(__name__)

SessionFactory = Callable[[], AsyncSession]
WakeCallback = Callable[[], None]

_USAGE_PROMPT = load_sibling_md(__file__, "reply.md")


class ReplyTool:
    """实现 Tool 协议。注入 session_factory（写事件）+ wake_reply_worker
    （唤醒 ReplySendWorker 立即出货）。
    """

    name = "reply"
    description = (
        "Send a message into the current scope's chat (group or private). "
        "In group chat, most messages are NOT addressed to you — call this "
        "tool only when you've decided to actively speak (e.g. you were "
        "@-mentioned, your past reply was quoted, the scope is private, or "
        "a clearly answerable question went unanswered). Skipping this tool "
        "and emitting `idle` is the correct choice for any tick where no one "
        "is addressing you. The arguments mirror the OneBot V11 send-message "
        "payload: content (list of segments), target (kind/group_id|user_id "
        "matching the current scope), and an optional related_msg_hashes."
    )
    usage_prompt = _USAGE_PROMPT
    # 发言不分群员等级；小奏在普通群员状态也能说话
    required_permission = PermissionTier.GUEST
    require_bot_admin = False
    arguments_schema = {
        "type": "object",
        "properties": {
            "content": {
                "type": "array",
                "description": (
                    "OneBot V11 segment list. Each item is "
                    "{\"type\": \"text|at|reply|face|...\", \"data\": {...}}. "
                    "See the tool's usage doc for the full grammar."
                ),
                "items": {"type": "object"},
            },
            "target": {
                "type": "object",
                "description": (
                    "{\"kind\": \"group\", \"group_id\": <int>} for group "
                    "scope, or {\"kind\": \"private\", \"user_id\": <int>} "
                    "for private scope. MUST match the current "
                    "<agent-input scope=\"...\">."
                ),
                "properties": {
                    "kind": {"type": "string", "enum": ["group", "private"]},
                    "group_id": {"type": "integer"},
                    "user_id": {"type": "integer"},
                },
                "required": ["kind"],
            },
            "related_msg_hashes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional; informational bookkeeping.",
                "default": [],
            },
        },
        "required": ["content", "target"],
    }

    def __init__(
        self,
        session_factory: SessionFactory,
        wake_reply_worker: WakeCallback | None = None,
    ) -> None:
        self._session_factory = session_factory
        # None 时退化为静默 —— 测试场景常用；生产由 v2_main 注入
        # supervisor.notify_reply_pending。注入时机：v2_main 先 build_default_registry
        # 拿到 ReplyTool（此时 supervisor 还没建），再 set_wake_callback 回填。
        self._wake: WakeCallback = wake_reply_worker or (lambda: None)

    def set_wake_callback(self, wake: WakeCallback) -> None:
        """v2_main 装配阶段用：supervisor 构造完成后回填 notify_reply_pending。

        Tool 协议本身不要求工具暴露此方法，这是 ReplyTool 特例（因为它
        与 LoopSupervisor 双向依赖：注册到 supervisor 的 tool_registry，
        又需要 supervisor 的 notify 回调）。
        """
        self._wake = wake

    async def run(self, arguments: dict, **context: Any) -> dict:
        scope_key = context.get("scope_key")
        if not scope_key or not isinstance(scope_key, str):
            raise ValueError("reply requires scope_key from caller context")
        correlation_id = context.get("correlation_id")
        if not correlation_id or not isinstance(correlation_id, str):
            raise ValueError(
                "reply requires correlation_id from caller context"
            )

        content = arguments.get("content")
        if not isinstance(content, list) or not content:
            raise ValueError("reply.content must be a non-empty list")

        target = arguments.get("target")
        if not isinstance(target, dict):
            raise ValueError("reply.target must be an object")

        err = _validate_target_against_scope(target, scope_key=scope_key)
        if err is not None:
            # 用一个带 error_kind 标签的异常让 ToolWorker 区分客户端错误
            # vs 内部异常；ToolWorker 当前不区分，但日志/排障更友好。
            raise ValueError(err)

        related = arguments.get("related_msg_hashes") or []
        if not isinstance(related, list):
            related = []

        reply_id = new_event_id()
        await write_agent_event(
            self._session_factory,
            event_type="agent.reply_emitted",
            scope_key=scope_key,
            correlation_id=correlation_id,
            # tool_called 那条事件的 event_id 不在 context 里（ToolWorker
            # 没传），因此这里 causation_id 留 None；下游靠 correlation_id
            # 串联同一 tick 内的所有事件。
            causation_id=None,
            payload={
                "reply_id": reply_id,
                "content": content,
                "target": target,
                "related_msg_hashes": list(related),
            },
        )

        try:
            self._wake()
        except Exception as exc:
            logger.warning("[reply_tool] wake callback failed: {}", exc)

        return {"reply_event_id": reply_id, "queued": True}


def _validate_target_against_scope(
    target: dict, *, scope_key: str
) -> str | None:
    """target.kind / target.group_id|user_id 必须严格匹配当前 loop 的 scope_key。
    返回 None = 通过；否则返回 error_message。

    防呆：避免 LLM 把 group:100 的回复打到 group:200 去。
    """
    try:
        scope, group_id, user_id = parse_scope_key(scope_key)
    except ValueError as exc:
        return f"invalid scope_key {scope_key!r}: {exc}"

    kind = target.get("kind")
    if kind != scope:
        return (
            f"target.kind={kind!r} does not match current scope={scope!r}"
        )
    if scope == "group":
        if target.get("group_id") != group_id:
            return (
                f"target.group_id={target.get('group_id')!r} does not match "
                f"current scope's group_id={group_id!r}"
            )
    elif scope == "private":
        if target.get("user_id") != user_id:
            return (
                f"target.user_id={target.get('user_id')!r} does not match "
                f"current scope's user_id={user_id!r}"
            )
    return None

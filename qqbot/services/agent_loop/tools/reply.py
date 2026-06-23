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
- 立即调用 context 里注入的 notify_reply_pending 回调唤醒 ReplySendWorker；
  回调缺失（测试 / supervisor 未就绪）时退化为不主动唤醒（worker 启动期的
  catchup 仍能兜底，仅延迟变长）
- 返回 {"reply_event_id": "...", "queued": true} 作为 tool_result

依赖注入方式（统一接口）：session_factory（写事件）与 notify_reply_pending
（唤醒 ReplySendWorker）都由 ToolWorker 在 run() 的 context 里注入，本工具
不再有自己的 __init__ / set_wake_callback —— 与 websearch / search_history
同构，系统不必按名字特判 reply。

旧 ReplyAction / `agent.reply_emitted` 事件类型 / ReplySendWorker 全部保留；
本工具只是接管"上游写 reply_emitted"这一职责。

契约：任务与决策契约.md §6 (reply 链路保持 reply_emitted → delivered/failed
不变；仅入口从 ReplyAction 改为 ReplyTool)
"""

from __future__ import annotations

from typing import Any

from qqbot.core.ids import new_event_id
from qqbot.core.logging import get_logger
from qqbot.services.agent_loop.event_writer import (
    parse_scope_key,
    write_agent_event,
)
from qqbot.services.agent_loop.prompts import load_sibling_md
from qqbot.services.agent_loop.tool_registry import BaseTool

logger = get_logger(__name__)

_USAGE_PROMPT = load_sibling_md(__file__, "reply.md")


class ReplyTool(BaseTool):
    """实现 Tool 协议。无构造依赖：session_factory（写事件）与
    notify_reply_pending（唤醒 ReplySendWorker 立即出货）都从 run() 的
    context 进，由 ToolWorker 统一注入。
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
    # required_permission / require_bot_admin 用 BaseTool 默认值
    # （GUEST / False）：发言不分群员等级，小奏普通群员也能说话。
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

    async def run(self, arguments: dict, **context: Any) -> dict:
        scope_key = context.get("scope_key")
        if not scope_key or not isinstance(scope_key, str):
            raise ValueError("reply requires scope_key from caller context")
        correlation_id = context.get("correlation_id")
        if not correlation_id or not isinstance(correlation_id, str):
            raise ValueError(
                "reply requires correlation_id from caller context"
            )
        # session_factory 由 ToolWorker 注入；缺失说明调用方没按统一接口
        # 注入系统依赖，直接 raise（ToolWorker 写 tool_failed）而不是
        # 让 write_agent_event 抛个晦涩的 None 错。
        session_factory = context.get("session_factory")
        if session_factory is None:
            raise ValueError(
                "reply requires session_factory from caller context"
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
            session_factory,
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

        # notify_reply_pending：ToolWorker 注入 supervisor.notify_reply_pending，
        # 让 ReplySendWorker 立即出货。缺失 / 非 callable 时静默跳过（catchup 兜底）。
        notify = context.get("notify_reply_pending")
        if callable(notify):
            try:
                notify()
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

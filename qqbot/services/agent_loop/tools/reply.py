"""reply 工具：向当前 scope 追加一条发言授权，或撤掉待发的那份。

2026-07-24（待办#19）起是 **append-only**：每次调用都是完整、自足的一条授权，
不引用上一次调用、程序也不做任何合并计算。scope 内仍只有一份 open reply_task
承载"等到什么时候发"，后续调用 append 上去并把 flush_at 换成自己的（最新一次
直接获胜，能延长也能缩短）。多条授权怎么综合成一段话，交给 flush 时的
Replyer——它拿到与 Planner 同权重的完整 timeline，每条授权都以
``<tool-call name="reply">`` 行留在上面。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from qqbot.core.ids import new_event_id
from qqbot.core.logging import get_logger
from qqbot.core.time import china_now
from qqbot.services.agent_loop.prompts import load_sibling_md
from qqbot.services.agent_loop.reply_task import (
    MAX_HOLD_SECONDS,
    append_cancel,
    append_upsert,
    build_upsert_payload,
    find_cancel_for_tool_call,
    find_upsert_for_tool_call,
    load_open_reply_task,
    scope_lock,
)
from qqbot.services.agent_loop.tool_registry import BaseTool, ToolOutcome
from qqbot.services.agent_loop.tools.send_message import _validate_content

_USAGE_PROMPT = load_sibling_md(__file__, "reply.md")
logger = get_logger(__name__)


class ReplyTool(BaseTool):
    name = "reply"
    allowed_scopes = ("group", "private")
    # reply-only 成功批次不应为“草稿已落定”再开一拍；失败仍需让 Planner 看见。
    wake_policy = "on_failure"
    description = (
        "Append one self-contained authorization to speak, or cancel the "
        "pending draft. Each call stands alone — never reference an earlier "
        "call; the newest hold_seconds wins outright. A successful call only "
        "stores pending intent; it does NOT mean anything was sent. Actual "
        "speech appears later as <my-reply> after runtime.reply_flushed."
    )
    usage_prompt = _USAGE_PROMPT
    arguments_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["upsert", "cancel"],
                "description": (
                    "upsert appends one authorization; cancel withdraws the "
                    "pending draft entirely."
                ),
            },
            "reply_task_id": {
                "type": "string",
                "description": (
                    "cancel only, and optional even there: omit to withdraw "
                    "whatever draft is pending. Never needed on upsert."
                ),
            },
            "mode": {
                "type": "string",
                "enum": ["compose", "verbatim"],
                "description": "compose by default; verbatim bypasses Replyer.",
            },
            "targets": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "message_id": {"type": ["string", "integer"]},
                        "sender_qq": {"type": ["string", "integer"]},
                        "context": {
                            "type": "string",
                            "description": (
                                "Conversation analysis for this message: who "
                                "is talking to whom and what it means in its "
                                "thread."
                            ),
                        },
                        "guidance": {
                            "type": "string",
                            "description": (
                                "How to respond: angle, approach, boundaries. "
                                "Not final wording."
                            ),
                        },
                    },
                    "required": ["context"],
                    "additionalProperties": False,
                },
            },
            "gist": {
                "type": "object",
                "properties": {
                    "situation": {
                        "type": "string",
                        "description": (
                            "Map of the room's current conversation threads."
                        ),
                    },
                    "intent": {"type": "string"},
                    "facts": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "avoid": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "tone": {"type": "string"},
                },
                "additionalProperties": False,
            },
            "hold_seconds": {
                "type": "integer",
                "minimum": 0,
                "maximum": MAX_HOLD_SECONDS,
                "description": (
                    "Required on upsert — no default. How long to hold this "
                    "draft before it is composed and sent. The newest call "
                    "wins outright: it may shorten as well as extend."
                ),
            },
            "verbatim_messages": {
                "type": "array",
                "minItems": 1,
                "maxItems": 4,
                "items": {
                    "type": "object",
                    "properties": {"content": {"type": "array"}},
                    "required": ["content"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["action"],
        "additionalProperties": False,
        "allOf": [
            {
                "if": {
                    "properties": {"action": {"const": "upsert"}},
                    "required": ["action"],
                },
                "then": {"required": ["hold_seconds"]},
            },
            {
                "if": {
                    "properties": {"mode": {"const": "verbatim"}},
                    "required": ["mode"],
                },
                "then": {"required": ["verbatim_messages"]},
            },
        ],
    }

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        if fail := await self.enforce_access(context):
            return fail
        scope_key = context.get("scope_key")
        session_factory = context.get("session_factory")
        correlation_id = context.get("correlation_id")
        tool_call_event_id = context.get("tool_call_event_id")
        if not all(
            isinstance(value, str) and value
            for value in (scope_key, correlation_id, tool_call_event_id)
        ) or session_factory is None:
            return ToolOutcome.failure(
                "internal_tool_error", "reply task persistence is not wired"
            )

        action = arguments.get("action")
        if action not in ("upsert", "cancel"):
            return _invalid("bad_action", "action must be upsert or cancel")
        async with scope_lock(scope_key):
            if action == "cancel":
                return await self._cancel(
                    arguments,
                    session_factory=session_factory,
                    scope_key=scope_key,
                    correlation_id=correlation_id,
                    tool_call_event_id=tool_call_event_id,
                )
            return await self._upsert(
                arguments,
                session_factory=session_factory,
                scope_key=scope_key,
                correlation_id=correlation_id,
                tool_call_event_id=tool_call_event_id,
                notify=context.get("notify_reply_task"),
            )

    async def _upsert(
        self,
        arguments: dict,
        *,
        session_factory: Any,
        scope_key: str,
        correlation_id: str,
        tool_call_event_id: str,
        notify: Any,
    ) -> ToolOutcome:
        # ToolWorker 在 domain event 已写、tool_result 未写之间崩溃时的幂等恢复。
        existing_payload = await find_upsert_for_tool_call(
            session_factory, tool_call_event_id
        )
        if existing_payload is not None:
            return ToolOutcome.success(_result_from_payload(existing_payload))

        # hold_seconds 无默认值（2026-07-24，待办#19）：等多久是每次调用都要
        # 现场判断的语义（"这个人说完了没"），给默认值等于替模型做决定——
        # 曾经的 default 0 就把合并窗口整个关掉了。
        if "hold_seconds" not in arguments:
            return _invalid(
                "missing_hold_seconds",
                "hold_seconds is required on upsert; there is no default",
            )
        hold = _coerce_hold(arguments.get("hold_seconds"))
        if hold is None:
            return _invalid(
                "bad_hold_seconds",
                f"hold_seconds must be an integer in [0, {MAX_HOLD_SECONDS}]",
            )
        mode = arguments.get("mode", "compose")
        if mode not in ("compose", "verbatim"):
            return _invalid("bad_mode", "mode must be compose or verbatim")
        targets, fail = _validate_targets(arguments.get("targets", []))
        if fail:
            return fail
        gist, fail = _validate_gist(arguments.get("gist", {}))
        if fail:
            return fail
        verbatim, fail = _validate_verbatim(
            arguments.get("verbatim_messages", []), mode
        )
        if fail:
            return fail

        # ─── append-only 授权（2026-07-24，待办#19）───
        # 每次调用就是一条完整、自足的授权，不引用上一次、不做合并计算：
        # 模型不再抄 reply_task_id/expected_revision，程序也不再 merge
        # targets/gist（旧 merge 只增不删，撤不掉写错的 target 与 fact）。
        # scope 内仍只有一份 open reply_task 承载"等到什么时候发"，后续调用
        # append 上去、把 flush_at 换成自己的——**最新一次调用直接获胜，能
        # 延长也能缩短**。内容怎么综合交给 flush 时的 Replyer：它拿的是与
        # Planner 同权重的完整 timeline，每条授权都以 <tool-call name="reply">
        # 行留在上面（投影不再折叠 reply 成功行）。
        now = china_now()
        current = await load_open_reply_task(session_factory, scope_key)
        if current is None:
            task_id = new_event_id()
            revision = 1
            created_at = now
            hard_deadline = now + timedelta(seconds=MAX_HOLD_SECONDS)
        else:
            # verbatim 独占：它绕过 Replyer 直发，没有"综合多条授权"可言。
            # 撞上就只能先 cancel 再重落。
            if current.mode != "compose" or mode != "compose":
                return ToolOutcome.failure(
                    "reply_task_locked",
                    "a verbatim reply_task is exclusive; cancel it first",
                    reply_task_id=current.reply_task_id,
                )
            task_id = current.reply_task_id
            revision = current.revision + 1
            created_at = current.created_at
            hard_deadline = current.hard_deadline
        # hard_deadline 自创建时刻起算、不随 append 滑动——它是"再怎么等也
        # 必须发出去"的硬上界。
        flush_at = min(now + timedelta(seconds=hold), hard_deadline)

        if mode == "compose" and not targets and not gist.get("intent"):
            return _invalid(
                "empty_reply_task", "compose reply requires targets or gist.intent"
            )
        payload = build_upsert_payload(
            reply_task_id=task_id,
            revision=revision,
            created_at=created_at,
            updated_at=now,
            flush_at=flush_at,
            hard_deadline=hard_deadline,
            mode=mode,
            targets=targets,
            gist=gist,
            verbatim_messages=verbatim,
        )
        event_id = await append_upsert(
            session_factory,
            scope_key=scope_key,
            correlation_id=correlation_id,
            tool_call_event_id=tool_call_event_id,
            payload=payload,
        )
        if notify is not None:
            try:
                await notify(scope_key, task_id, revision, flush_at, event_id)
            except Exception as exc:
                # 落稿事件已经是完成真值；调度通知失败不能把已成功的工具调用
                # 反写成失败。后续拍仍能从 timeline 上自己那条 <tool-call
                # name="reply"> 行看见并追授权/撤销，重启 rescan 也会重挂未来
                # 定时器。
                logger.warning(
                    "[reply] persisted task {} but scheduling failed: {}",
                    task_id,
                    exc,
                )
        return ToolOutcome.success(_result_from_payload(payload))

    async def _cancel(
        self,
        arguments: dict,
        *,
        session_factory: Any,
        scope_key: str,
        correlation_id: str,
        tool_call_event_id: str,
    ) -> ToolOutcome:
        existing = await find_cancel_for_tool_call(
            session_factory, tool_call_event_id
        )
        if existing is not None:
            return ToolOutcome.success(
                {
                    "reply_task_id": existing.get("reply_task_id"),
                    "revision": existing.get("revision"),
                    "state": "cancelled",
                }
            )
        # reply_task_id 可省（2026-07-24，待办#19）：scope 内至多一份 open
        # reply_task，"撤掉待发的那份"无歧义。给了就必须对得上——防止模型拿
        # 着一份已经发出去的旧 id 来撤，静默撤错另一份。expected_revision 随
        # append 语义一并取消：授权是追加的，没有需要 CAS 的合并冲突。
        requested_id = arguments.get("reply_task_id")
        if requested_id is not None and (
            not isinstance(requested_id, str) or not requested_id
        ):
            return _invalid("bad_reply_task_id", "reply_task_id must be a string")
        task = await load_open_reply_task(session_factory, scope_key)
        if task is None or (
            requested_id is not None and task.reply_task_id != requested_id
        ):
            return ToolOutcome.failure(
                "reply_task_not_found", "no open reply_task to cancel"
            )
        task_id = task.reply_task_id
        await append_cancel(
            session_factory,
            scope_key=scope_key,
            correlation_id=correlation_id,
            tool_call_event_id=tool_call_event_id,
            task=task,
        )
        return ToolOutcome.success(
            {
                "reply_task_id": task_id,
                "revision": task.revision,
                "state": "cancelled",
            }
        )


def _coerce_hold(raw: Any) -> int | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int) and 0 <= raw <= MAX_HOLD_SECONDS:
        return raw
    if isinstance(raw, str) and raw.strip().isdigit():
        value = int(raw.strip())
        return value if 0 <= value <= MAX_HOLD_SECONDS else None
    return None


def _validate_targets(raw: Any) -> tuple[list[dict], ToolOutcome | None]:
    """2026-07-22 语义换代：points（要点清单，"教 Replyer 说什么"）→
    context + guidance（对话关系分析 + 回法，"教 Replyer 怎么回"）。
    Replyer 已与 Planner 看同一份 timeline，不需要转述内容；旧 points 形态
    直接拒绝并给出可读 reason_code，让 Planner 下一拍照错误自纠。"""
    if not isinstance(raw, list):
        return [], _invalid("bad_targets", "targets must be an array")
    out: list[dict] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            return [], _invalid("bad_target", f"targets[{index}] must be an object")
        if "points" in item:
            return [], _invalid(
                "points_replaced_by_context",
                f"targets[{index}].points is retired: describe the "
                "conversation in `context` and how to respond in `guidance`",
            )
        context_val = item.get("context")
        if not isinstance(context_val, str) or not context_val.strip():
            return [], _invalid(
                "bad_target_context",
                f"targets[{index}].context must be a non-empty string",
            )
        normalized: dict[str, Any] = {"context": context_val.strip()}
        guidance = item.get("guidance")
        if guidance is not None and not isinstance(guidance, str):
            return [], _invalid(
                "bad_target_guidance",
                f"targets[{index}].guidance must be a string",
            )
        if isinstance(guidance, str) and guidance.strip():
            normalized["guidance"] = guidance.strip()
        for key in ("message_id", "sender_qq"):
            value = item.get(key)
            if value is not None:
                normalized[key] = str(value)
        out.append(normalized)
    return out, None


def _validate_gist(raw: Any) -> tuple[dict, ToolOutcome | None]:
    if not isinstance(raw, dict):
        return {}, _invalid("bad_gist", "gist must be an object")
    out: dict[str, Any] = {}
    for key in ("situation", "intent", "tone"):
        value = raw.get(key)
        if value is not None and not isinstance(value, str):
            return {}, _invalid("bad_gist", f"gist.{key} must be a string")
        if isinstance(value, str) and value.strip():
            out[key] = value.strip()
    for key in ("facts", "avoid"):
        value = raw.get(key, [])
        if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
            return {}, _invalid("bad_gist", f"gist.{key} must be an array of strings")
        out[key] = [v.strip() for v in value if v.strip()]
    return out, None


def _validate_verbatim(raw: Any, mode: str) -> tuple[list[dict], ToolOutcome | None]:
    if mode == "compose":
        if raw not in (None, []):
            return [], _invalid(
                "verbatim_not_applicable",
                "compose mode cannot include verbatim_messages",
            )
        return [], None
    if not isinstance(raw, list) or not raw:
        return [], _invalid("empty_verbatim", "verbatim mode requires messages")
    if len(raw) > 4:
        return [], _invalid("too_many_messages", "at most 4 verbatim messages")
    out: list[dict] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            return [], _invalid(
                "bad_verbatim",
                f"verbatim_messages[{index}] must be an object",
            )
        content = item.get("content")
        if fail := _validate_content(content):
            return [], fail
        out.append({"content": content})
    return out, None


def _invalid(reason_code: str, message: str) -> ToolOutcome:
    return ToolOutcome.failure(
        "invalid_arguments",
        message,
        reason_code=reason_code,
        retryable=False,
        transient=False,
        user_fixable=True,
    )


def _result_from_payload(payload: dict) -> dict:
    return {
        "reply_task_id": payload.get("reply_task_id"),
        "revision": payload.get("revision"),
        "state": payload.get("state", "open"),
        "flush_at": payload.get("flush_at"),
        "hard_deadline": payload.get("hard_deadline"),
    }

"""reply 工具：向当前 scope 的 ReplyTask 内容区落稿、合稿或撤稿。"""

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
    load_reply_task,
    merge_gist,
    merge_targets,
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
        "Create, merge, or cancel the current scope's short-lived reply_task. "
        "A successful call only stores pending reply intent; it does NOT mean "
        "anything was sent. Actual speech appears later as <my-reply> after "
        "runtime.reply_flushed."
    )
    usage_prompt = _USAGE_PROMPT
    arguments_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["upsert", "cancel"],
                "description": "Create/merge a draft, or cancel the open draft.",
            },
            "reply_task_id": {
                "type": "string",
                "description": "Omit on create; copy from pending-reply on merge/cancel.",
            },
            "expected_revision": {
                "type": "integer",
                "minimum": 1,
                "description": "Required on merge/cancel; copy the current revision.",
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
                        "points": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["points"],
                    "additionalProperties": False,
                },
            },
            "gist": {
                "type": "object",
                "properties": {
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
                "if": {"properties": {"action": {"const": "cancel"}}},
                "then": {"required": ["reply_task_id", "expected_revision"]},
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

        hold = _coerce_hold(arguments.get("hold_seconds", 0))
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

        now = china_now()
        requested_id = arguments.get("reply_task_id")
        current = await load_open_reply_task(session_factory, scope_key)
        if requested_id is None:
            if current is not None:
                return ToolOutcome.failure(
                    "reply_task_exists",
                    "an open reply_task already exists; merge with its id/revision",
                    reply_task_id=current.reply_task_id,
                    revision=current.revision,
                )
            task_id = new_event_id()
            revision = 1
            created_at = now
            hard_deadline = now + timedelta(seconds=MAX_HOLD_SECONDS)
            flush_at = now + timedelta(seconds=hold)
            merged_targets = targets
            merged_gist = gist
        else:
            if not isinstance(requested_id, str) or not requested_id:
                return _invalid("bad_reply_task_id", "reply_task_id must be a string")
            if current is None or current.reply_task_id != requested_id:
                return ToolOutcome.failure(
                    "reply_task_not_found", "open reply_task was not found"
                )
            expected = arguments.get("expected_revision")
            if expected != current.revision:
                return ToolOutcome.failure(
                    "reply_task_revision_conflict",
                    "expected_revision does not match current revision",
                    expected_revision=expected,
                    actual_revision=current.revision,
                )
            if current.mode != "compose" or mode != "compose":
                return ToolOutcome.failure(
                    "reply_task_locked", "verbatim reply_task cannot be merged"
                )
            task_id = current.reply_task_id
            revision = current.revision + 1
            created_at = current.created_at
            hard_deadline = current.hard_deadline
            requested_flush = now + timedelta(seconds=hold)
            flush_at = min(max(current.flush_at, requested_flush), hard_deadline)
            merged_targets = merge_targets(current.targets, targets)
            merged_gist = merge_gist(current.gist, gist)

        if mode == "compose" and not merged_targets and not merged_gist.get("intent"):
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
            targets=merged_targets,
            gist=merged_gist,
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
                # 反写成失败。后续拍仍能从 <pending-reply> 看见并合并/撤销，
                # 重启 rescan 也会重挂未来定时器。
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
        task_id = arguments.get("reply_task_id")
        if not isinstance(task_id, str) or not task_id:
            return _invalid("bad_reply_task_id", "cancel requires reply_task_id")
        task = await load_reply_task(session_factory, scope_key, task_id)
        if task is None:
            return ToolOutcome.failure(
                "reply_task_not_found", "reply_task was not found"
            )
        if task.state != "open":
            return ToolOutcome.failure(
                "reply_task_locked", f"reply_task state is {task.state}"
            )
        expected = arguments.get("expected_revision")
        if expected != task.revision:
            return ToolOutcome.failure(
                "reply_task_revision_conflict",
                "expected_revision does not match current revision",
                expected_revision=expected,
                actual_revision=task.revision,
            )
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
    if not isinstance(raw, list):
        return [], _invalid("bad_targets", "targets must be an array")
    out: list[dict] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            return [], _invalid("bad_target", f"targets[{index}] must be an object")
        points = item.get("points", [])
        if not isinstance(points, list) or any(not isinstance(p, str) for p in points):
            return [], _invalid(
                "bad_target_points",
                f"targets[{index}].points must be strings",
            )
        normalized = {"points": [p.strip() for p in points if p.strip()]}
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
    for key in ("intent", "tone"):
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

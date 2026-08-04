"""TaskTool —— 在当前 AgentLoop tick 内维护跨拍任务状态。"""

from __future__ import annotations

from typing import Any

from qqbot.core.ids import new_event_id
from qqbot.services.agent_loop.prompts import load_sibling_md
from qqbot.services.agent_loop.tool_registry import (
    BaseTool,
    ToolGeneratedEvent,
    ToolOutcome,
)

_USAGE_PROMPT = load_sibling_md(__file__, "task.md")

_SHORT_TEXT_MAX_LENGTH = 200
_DETAIL_TEXT_MAX_LENGTH = 1000

_CREATE_FIELDS = {
    "action",
    "description",
    "related_tools",
    "parent_task_id",
}
_NOTE_FIELDS = {"action", "task_id", "note"}
_COMPLETE_FIELDS = {"action", "task_id", "result_summary"}
_FAIL_FIELDS = {"action", "task_id", "reason"}


class TaskTool(BaseTool):
    """把 Task 生命周期收敛成一个 effect 程序函数。"""

    name = "task"
    program_kind = "effect"
    max_call_sites = 2
    description = (
        "创建并维护可跨 tick 持续存在的任务。action 支持 create、note、"
        "complete 和 fail。create 返回的 task_id 可直接存入程序变量，供后续"
        "effect 的 task_id= 参数使用。"
    )
    usage_prompt = _USAGE_PROMPT
    arguments_schema = {
        "type": "object",
        "oneOf": [
            {
                "properties": {
                    "action": {
                        "const": "create",
                        "description": "固定为 create，表示创建任务。",
                    },
                    "description": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": _SHORT_TEXT_MAX_LENGTH,
                        "description": "任务目标描述。",
                    },
                    "related_tools": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                        "description": "与任务相关的工具名列表。",
                    },
                    "parent_task_id": {
                        "type": ["string", "null"],
                        "minLength": 1,
                        "description": "父任务 ID；无父任务时可省略或传 null。",
                    },
                },
                "required": ["action", "description"],
                "additionalProperties": False,
            },
            {
                "properties": {
                    "action": {
                        "const": "note",
                        "description": "固定为 note，表示追加进度记录。",
                    },
                    "task_id": {
                        "type": "string",
                        "minLength": 1,
                        "description": "目标任务 ID。",
                    },
                    "note": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": _SHORT_TEXT_MAX_LENGTH,
                        "description": "写入任务的进度记录。",
                    },
                },
                "required": ["action", "task_id", "note"],
                "additionalProperties": False,
            },
            {
                "properties": {
                    "action": {
                        "const": "complete",
                        "description": "固定为 complete，表示完成任务。",
                    },
                    "task_id": {
                        "type": "string",
                        "minLength": 1,
                        "description": "目标任务 ID。",
                    },
                    "result_summary": {
                        "type": ["string", "null"],
                        "minLength": 1,
                        "maxLength": _DETAIL_TEXT_MAX_LENGTH,
                        "description": "可选的任务结果摘要。",
                    },
                },
                "required": ["action", "task_id"],
                "additionalProperties": False,
            },
            {
                "properties": {
                    "action": {
                        "const": "fail",
                        "description": "固定为 fail，表示任务失败。",
                    },
                    "task_id": {
                        "type": "string",
                        "minLength": 1,
                        "description": "目标任务 ID。",
                    },
                    "reason": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": _DETAIL_TEXT_MAX_LENGTH,
                        "description": "任务失败原因。",
                    },
                },
                "required": ["action", "task_id", "reason"],
                "additionalProperties": False,
            },
        ],
    }
    result_schema = {
        "type": "object",
        "properties": {
            "action": {"type": "string"},
            "task_id": {"type": "string"},
            "state": {"type": "string"},
        },
        "required": ["action", "task_id", "state"],
        "additionalProperties": False,
    }

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        if fail := await self.enforce_access(context):
            return fail
        if not isinstance(arguments, dict):
            return _invalid("arguments_not_object", "arguments must be an object")

        action = arguments.get("action")
        if action == "create":
            return _create(arguments, context)
        if action == "note":
            return _note(arguments)
        if action == "complete":
            return _complete(arguments)
        if action == "fail":
            return _fail(arguments)
        return _invalid(
            "unknown_action",
            "action must be one of: create, note, complete, fail",
            action=action,
        )


def _create(arguments: dict, context: dict) -> ToolOutcome:
    if fail := _reject_unknown(arguments, _CREATE_FIELDS):
        return fail
    description = _required_text(
        arguments.get("description"),
        "description",
        max_length=_SHORT_TEXT_MAX_LENGTH,
    )
    if isinstance(description, ToolOutcome):
        return description
    related_tools = arguments.get("related_tools", [])
    if not isinstance(related_tools, list) or any(
        not isinstance(item, str) or not item.strip() for item in related_tools
    ):
        return _invalid(
            "related_tools_not_string_list",
            "related_tools must be a list of non-empty strings",
        )
    parent_task_id = _optional_text(
        arguments.get("parent_task_id"), "parent_task_id"
    )
    if isinstance(parent_task_id, ToolOutcome):
        return parent_task_id
    task_id = new_event_id()
    event = ToolGeneratedEvent(
        event_type="agent.task_created",
        payload={
            "task_id": task_id,
            "description": description,
            "related_tools": [item.strip() for item in related_tools],
            "parent_task_id": parent_task_id,
            "triggered_by_event_id": context.get("triggered_by_event_id"),
        },
    )
    return ToolOutcome.success(
        {
            "action": "create",
            "task_id": task_id,
            "state": "pending",
        },
        emitted_events=[event],
    )


def _note(arguments: dict) -> ToolOutcome:
    if fail := _reject_unknown(arguments, _NOTE_FIELDS):
        return fail
    task_id = _required_text(arguments.get("task_id"), "task_id")
    if isinstance(task_id, ToolOutcome):
        return task_id
    note = _required_text(
        arguments.get("note"),
        "note",
        max_length=_SHORT_TEXT_MAX_LENGTH,
    )
    if isinstance(note, ToolOutcome):
        return note
    event = ToolGeneratedEvent(
        event_type="agent.task_progress_noted",
        payload={"task_id": task_id, "note": note},
    )
    return ToolOutcome.success(
        {"action": "note", "task_id": task_id, "state": "unchanged"},
        emitted_events=[event],
    )


def _complete(arguments: dict) -> ToolOutcome:
    if fail := _reject_unknown(arguments, _COMPLETE_FIELDS):
        return fail
    task_id = _required_text(arguments.get("task_id"), "task_id")
    if isinstance(task_id, ToolOutcome):
        return task_id
    summary = _optional_text(
        arguments.get("result_summary"),
        "result_summary",
        max_length=_DETAIL_TEXT_MAX_LENGTH,
    )
    if isinstance(summary, ToolOutcome):
        return summary
    event = ToolGeneratedEvent(
        event_type="agent.task_state_changed",
        payload={
            "task_id": task_id,
            "to_state": "done",
            "reason": summary,
        },
    )
    return ToolOutcome.success(
        {"action": "complete", "task_id": task_id, "state": "done"},
        emitted_events=[event],
    )


def _fail(arguments: dict) -> ToolOutcome:
    if fail := _reject_unknown(arguments, _FAIL_FIELDS):
        return fail
    task_id = _required_text(arguments.get("task_id"), "task_id")
    if isinstance(task_id, ToolOutcome):
        return task_id
    reason = _required_text(
        arguments.get("reason"),
        "reason",
        max_length=_DETAIL_TEXT_MAX_LENGTH,
    )
    if isinstance(reason, ToolOutcome):
        return reason
    event = ToolGeneratedEvent(
        event_type="agent.task_state_changed",
        payload={
            "task_id": task_id,
            "to_state": "failed",
            "reason": reason,
        },
    )
    return ToolOutcome.success(
        {"action": "fail", "task_id": task_id, "state": "failed"},
        emitted_events=[event],
    )


def _reject_unknown(arguments: dict, allowed: set[str]) -> ToolOutcome | None:
    unknown = sorted(set(arguments) - allowed)
    if not unknown:
        return None
    return _invalid(
        "unknown_fields",
        f"unknown field(s) for action={arguments.get('action')!r}: {unknown}",
        fields=unknown,
    )


def _required_text(
    value: Any,
    field: str,
    *,
    max_length: int | None = None,
) -> str | ToolOutcome:
    if not isinstance(value, str) or not value.strip():
        return _invalid(
            f"{field}_required",
            f"{field} must be a non-empty string",
        )
    text = value.strip()
    if max_length is not None and len(text) > max_length:
        return _invalid(
            f"{field}_too_long",
            f"{field} must be at most {max_length} characters",
            max_length=max_length,
        )
    return text


def _optional_text(
    value: Any,
    field: str,
    *,
    max_length: int | None = None,
) -> str | ToolOutcome | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        return _invalid(
            f"{field}_invalid",
            f"{field} must be null or a non-empty string",
        )
    text = value.strip()
    if max_length is not None and len(text) > max_length:
        return _invalid(
            f"{field}_too_long",
            f"{field} must be at most {max_length} characters",
            max_length=max_length,
        )
    return text


def _invalid(reason_code: str, message: str, **extra: Any) -> ToolOutcome:
    return ToolOutcome.failure(
        "invalid_arguments",
        message,
        reason_code=reason_code,
        **extra,
    )

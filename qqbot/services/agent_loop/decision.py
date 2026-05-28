"""Decision context / action types / planner protocol.

Contract: 开发文档/v2.0/任务与决策契约.md §2-§4

Action union currently only contains IdleAction (skeleton); create_task /
call_tool / reply / complete_task / fail_task will be added when the real
planner comes online.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol


@dataclass(frozen=True)
class IdleAction:
    """LLM (or skeleton planner) decided no work this tick."""

    type: str = "idle"
    reason: str = ""


@dataclass(frozen=True)
class CreateTaskAction:
    """Spawn a new task. `task_ref` is an in-tick alias the LLM uses so a
    later CallToolAction in the same `actions` list can reference this
    task before the loop has minted a real task_id.

    `triggered_by_event_id` is the LLM-supplied anchor of "what event made
    me create this task" — used by search_history to look up earlier context
    relative to the task's birthplace. Optional; tools degrade gracefully.
    """

    description: str
    related_tools: list[str] = field(default_factory=list)
    parent_task_id: str | None = None
    task_ref: str | None = None
    triggered_by_event_id: str | None = None
    type: str = "create_task"


@dataclass(frozen=True)
class CallToolAction:
    """Dispatch a tool call. Either `task_id` or `task_ref` may be used to
    attach the call to a task; both omitted means a lightweight call."""

    tool_name: str
    arguments: dict = field(default_factory=dict)
    task_id: str | None = None
    task_ref: str | None = None
    type: str = "call_tool"


@dataclass(frozen=True)
class CompleteTaskAction:
    task_id: str
    result_summary: str | None = None
    type: str = "complete_task"


@dataclass(frozen=True)
class FailTaskAction:
    task_id: str
    reason: str = ""
    type: str = "fail_task"


@dataclass(frozen=True)
class NoteTaskProgressAction:
    """Append a progress note to an existing task WITHOUT changing its
    state. Used so the LLM can "think out loud" across ticks: each tick
    sees a short tail of these notes per task and can build on its earlier
    reasoning instead of restarting from the timeline alone."""

    task_id: str
    note: str
    type: str = "note_task_progress"


# Union of every action type the loop translates. Order matters only for
# isinstance dispatch readability, not semantics.
#
# 注意：reply 不在这里 —— v2 中"发言"是 reply 工具的 CallToolAction，
# 不再是一类独立的 Action。这是为了让 LLM 把"要不要说话"当成一次工具调
# 用决策，与调 websearch / search_history 同构，避免被旧 ReplyAction 诱
# 导成"群里每条消息都要选择回 vs idle"的二分法。
Action = (
    IdleAction
    | CreateTaskAction
    | CallToolAction
    | CompleteTaskAction
    | FailTaskAction
    | NoteTaskProgressAction
)


@dataclass(frozen=True)
class DecisionOutput:
    actions: list[Action]
    reasoning: str | None = None


# ─── Projection-fed view dataclasses (任务与决策契约 §2.3, §4.1, §5.1) ───


@dataclass(frozen=True)
class ImageRef:
    """已下载落盘的图片素材引用。

    projection 把 message/agent_reply 里 downloaded=true 的 image segment
    收集到 TimelineItem.images 上，llm_planner 再据此从 local_path 读
    bytes、base64 编码、按 hash 去重塞进 multimodal content block。
    downloaded=false 的图不进 ImageRef（只在 render 文本里留占位）。
    """

    file_hash: str
    local_path: str
    mime: str


@dataclass(frozen=True)
class TimelineItem:
    """One renderable row in the LLM context (任务与决策契约 §2.3)."""

    event_id: str
    occurred_at: datetime
    kind: Literal["message", "notice", "tool_call", "agent_reply", "system_hint"]
    render: str
    related_event_ids: list[str] = field(default_factory=list)
    images: list[ImageRef] = field(default_factory=list)


@dataclass(frozen=True)
class ProgressNote:
    """A timestamped LLM-authored note attached to a task; folded from
    agent.task_progress_noted events."""

    at: datetime
    note: str


@dataclass(frozen=True)
class TaskView:
    """Folded task state from agent.task_* events (任务与决策契约 §4.1)."""

    task_id: str
    scope_key: str
    description: str
    related_tools: list[str]
    parent_task_id: str | None
    state: Literal["pending", "running", "done", "failed"]
    created_at: datetime
    last_changed_at: datetime
    last_change_reason: str | None
    pending_tool_call_ids: list[str]
    triggered_by_event_id: str | None = None
    progress_notes: list[ProgressNote] = field(default_factory=list)


@dataclass(frozen=True)
class ToolResultView:
    """A folded view of an agent.tool_called and its eventual result/failure
    (任务与决策契约 §5.1).
    """

    tool_call_id: str
    tool_name: str
    status: Literal["pending", "succeeded", "failed"]
    arguments: dict
    result: Any | None
    error_kind: str | None
    error_message: str | None


@dataclass(frozen=True)
class DecisionContext:
    scope_key: str
    correlation_id: str
    tick_seq: int
    now: datetime

    timeline: list[TimelineItem] = field(default_factory=list)
    active_tasks: list[TaskView] = field(default_factory=list)
    pending_tool_results: list[ToolResultView] = field(default_factory=list)

    # Reserved for the tool layer and runtime reflector; not populated by
    # the projection layer (they come from separate sources).
    tool_catalog: list[Any] = field(default_factory=list)
    runtime_hints: list[Any] = field(default_factory=list)

    # 当前 tick 上 bot 自己的 QQ user_id（由 bot_registry 提供，AgentLoop
    # 在 tick() 时 resolve 后注入）。None 表示 bot 还没连接 napcat / 注册
    # 第一条事件 —— prompt 渲染时不输出该属性，模型回退到"靠引用反推"。
    bot_user_id: str | None = None


class Planner(Protocol):
    """Stateless decision function.

    Implementations:
    - FakeIdlePlanner — skeleton; always idle.
    - (future) LLMPlanner — calls LLM① and parses DecisionOutput JSON.
    """

    async def decide(self, context: DecisionContext) -> DecisionOutput: ...

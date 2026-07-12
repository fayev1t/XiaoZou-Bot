"""Decision context / action types / planner protocol.

Contract: 开发文档/v2.0/任务与决策契约.md §2-§4

Action union currently only contains IdleAction (skeleton); create_task /
call_tool / complete_task / fail_task will be added when the real
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
    attach the call to a task; both omitted means a lightweight call.

    `triggered_by_event_id` 是 LLM 显式声明"是哪条事件让我调这个工具"。对
    敏感工具（required_permission > GUEST）**工具内** enforce_permission 据此
    **实时**解析触发用户的当前群角色 → tier 做权限校验；缺失时视作 GUEST，敏感
    工具自然失败。非敏感工具（send_message / websearch）可省略，仅作 audit 用。AgentLoop
    自己不解析 tier、不做任何权限判定——只把这个 anchor 原样注入
    tool_called.payload 交给工具；CallToolAction 缺省且 task 上挂了
    triggered_by_event_id 时，由 AgentLoop fall back 到 task 的 anchor 补全因果链。
    """

    tool_name: str
    arguments: dict = field(default_factory=dict)
    task_id: str | None = None
    task_ref: str | None = None
    triggered_by_event_id: str | None = None
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
# 注意：发言不在这里 —— v2 中"发言"是 send_message 工具的 CallToolAction，
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

    projection 把 message 里 downloaded=true 的 image segment
    收集到 TimelineItem.images 上，llm_planner 再据此从 local_path 读
    bytes、base64 编码、按 hash 去重塞进 multimodal content block。
    downloaded=false 的图不进 ImageRef（只在 render 文本里留占位）。
    """

    file_hash: str
    local_path: str
    mime: str


@dataclass(frozen=True)
class MemeView:
    """一条表情包收藏（agent_memes 读出的视图）。

    Projector 经 meme_store.load_saved_memes 挂到 DecisionContext.saved_memes，
    llm_planner 渲染成 <saved-memes> 里的一行 <meme hash="..." saved_at="...">
    描述</meme>。description 由收录（meme.save）时的 caption LLM 调用生成，是
    发送（meme.send）选图的唯一依据；hash 与 timeline <image hash="..."/> 同一
    值空间。

    context_note 是收录时留档的聊天语境（表情包工具黑盒设计.md §2"留档备将来
    重生成"）：meme.recaption 不带新语境时沿用它重跑 caption。**不进 prompt**
    ——<saved-memes> 只渲染 description。
    """

    file_hash: str
    description: str
    saved_at: datetime
    context_note: str | None = None


@dataclass(frozen=True)
class TimelineItem:
    """One renderable row in the LLM context (任务与决策契约 §2.3)."""

    event_id: str
    occurred_at: datetime
    kind: Literal[
        "message",
        "notice",
        "tool_call",
        "system_hint",
        "request",
        "task_closed",
        "my_thought",
    ]
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

    工具对模型只暴露**两态**：
    - ``processing`` —— 只有 agent.tool_called，还没等到 terminal 事件；
    - ``complete``   —— 已 terminal。成功/失败靠内容区分：``error_kind is
      None`` 为成功（``result`` 有效），非 None 为失败（error_* 有效）。
    旧三态 pending/succeeded/failed 已收敛：成败是 complete 的两种**内容**，
    不是两种**状态**——状态只回答"这次调用整体结束没有"。
    """

    tool_call_id: str
    tool_name: str
    status: Literal["processing", "complete"]
    arguments: dict
    result: Any | None
    error_kind: str | None
    error_message: str | None
    # 失败时 ToolOutcome.extra 平铺进 agent.tool_failed.payload 顶层的结构化附加
    # 字段（required_tier / actual_tier / required_bot_role / actual_bot_role /
    # retcode / action / allowed_scopes ...）。渲染时随 <error> 属性透给 LLM，让
    # 它能精确解释"差在哪一级权限 / napcat 具体报了什么"，而非只看一段 message。
    # None = 无附加字段或非失败态。
    error_extra: dict | None = None


@dataclass(frozen=True)
class DecisionContext:
    scope_key: str
    correlation_id: str
    tick_seq: int
    now: datetime

    timeline: list[TimelineItem] = field(default_factory=list)
    active_tasks: list[TaskView] = field(default_factory=list)

    # ─── 表情包收藏夹（2026-07-03 收发上线；2026-07-12 起为 meme 单工具）───
    # 全局共享的 agent_memes（2026-07-06 起全 bot 一份，created_at 倒序、
    # 封顶 meme_store.MAX_SAVED_MEMES 条），由 Projector.
    # _augment_with_saved_memes 注入；渲染成 <saved-memes>，meme 工具凭
    # 其中的 hash 精确发送/删除/换描述。空 = 不渲染。
    saved_memes: list[MemeView] = field(default_factory=list)
    # 2026-07-02 起不再有独立的 pending_tool_results 字段：工具结果只在
    # timeline 的 <tool-call status="complete"> 行呈现一次（单一事实源）。
    # 旧的"待消费工具更新区"实现从未做过消费切割——窗口内所有 complete 每拍
    # 重复以"待你处理"的名义出现，是复读的直接诱饵；且同一调用在 timeline
    # 与 pending 区双重渲染，两处语义必然漂移。ToolResultView 仍保留——它是
    # timeline 渲染 tool-call 行时的折叠视图（fold_tool_results）。

    # ─── 模型的跨拍自我记忆（2026-07-02 增 last_reasoning；2026-07-06 改为
    # 思考轨迹内联，待办清单#4）───
    # agent.decision_emitted 不再折叠成独立字段：build_timeline 直接把最近
    # MAX_THOUGHT_ROWS 条（含 idle 拍、空白 reasoning 跳过）渲染为 timeline
    # 的 <my-thought> 行，单条截 MAX_THOUGHT_CHARS 字、不挤占消息行预算。
    # 旧的 last_reasoning / last_reasoning_at 字段与 <last-reasoning> 区块已
    # 删除——只有 1 拍深的记忆使跨拍链条强度取决于最弱一拍，且与 <my-thought>
    # 并存会双重渲染最新一条。

    # ─── 同 tick 校验重试的反馈（任务与决策契约 §7.1）───
    # 上一次 decide() 输出未通过动作校验时，loop 带着错误描述重试；planner
    # 渲染成 <validation-error>。正常首次调用恒为 None，不进渲染。
    validation_feedback: str | None = None

    # 当前 tick 上 bot 自己的 QQ user_id（由 bot_registry 提供,AgentLoop
    # 在 tick() 时 resolve 后注入）。None 表示 bot 还没连接 napcat / 注册
    # 第一条事件 —— prompt 渲染时不输出该属性，模型回退到"靠引用反推"。
    bot_user_id: str | None = None

    # 当前 tick 在该 group scope 下小奏自己的群角色（owner / admin / member）。
    # 由 Projector.fold_bot_role() 从 runtime.bot_role_observed 事件折出最新值，
    # AgentLoop 在 dispatch 时原样注入 tool_called.payload.bot_role。它有两个用途：
    # ① 渲染成 <agent-input bot_role="..."> 供 LLM 判断自己能不能调需要角色的工具；
    # ② 作为工具内 enforce_bot_admin 的**回退快照**——真正判权限时工具会先
    #    **实时**向 napcat 查 bot 当前角色（_effective_bot_role），查不到才回退到它。
    # None = 未观测到（启动初期 sweep 未跑完 / 该群从未写过 baseline）——渲染时不输出
    # 该属性；工具侧若实时查也拿不到，则保守拒绝带 required_bot_role 的调用。
    bot_role: Literal["owner", "admin", "member"] | None = None


class Planner(Protocol):
    """Stateless decision function.

    Implementations:
    - FakeIdlePlanner — skeleton; always idle.
    - (future) LLMPlanner — calls LLM① and parses DecisionOutput JSON.
    """

    async def decide(self, context: DecisionContext) -> DecisionOutput: ...

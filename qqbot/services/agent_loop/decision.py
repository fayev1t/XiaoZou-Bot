"""Decision context and Planner protocol for program-shaped decisions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol


@dataclass(frozen=True)
class DecisionOutput:
    program: str
    raw_response: str | None = None
    planner_error: str | None = None


@dataclass(frozen=True)
class ProgramValidationFeedback:
    attempt: int
    error_kind: str
    message: str
    rejected_program: str
    line: int | None = None
    column: int | None = None


# ─── Projection-fed view dataclasses (任务与决策契约 §2.1、§8) ───


@dataclass(frozen=True)
class ImageRef:
    """已下载落盘的图片素材引用。

    projection 把 message 里 downloaded=true 的 image segment 收集到
    TimelineItem.images 上。downloaded=false 的图不进 ImageRef（只在 render
    文本里留占位）。

    2026-07-28 起**没有任何 prompt 装配路径消费它**：Planner/Replyer 已是纯
    文本模型，图片语义经 ingest 期写入的 desc= 属性随 render 文本抵达（见
    services/agent_loop/image_description.py）。保留本结构是因为它仍是"这条
    消息带了哪些已落盘图片"的结构化记录，读盘取像素的活现在只有
    look_at_image 工具做，且它按 hash 自己定位文件、不走这里。
    """

    file_hash: str
    local_path: str
    mime: str


@dataclass(frozen=True)
class MemeView:
    """一条表情包收藏（agent_memes 读出的视图）。

    Projector 经 meme_store.load_saved_memes 挂到 DecisionContext.saved_memes，
    llm_planner 渲染成信封 `## 表情包收藏` 一节里的一行
    ``<meme>hash12 (MM-DD): 描述``。description 由收录（meme.save）时的
    caption LLM 调用生成，是 Planner 经 send_messages 发图时选图的唯一依据；
    hash 与时间线 `<图 hash12 …>` 同一值空间（展示 12 位前缀，库存完整 64 位）。

    context_note 是收录时留档的聊天语境（表情包工具黑盒设计.md §2"留档备将来
    重生成"）：meme.recaption 不带新语境时沿用它重跑 caption。**不进 prompt**
    ——`## 表情包收藏` 节只渲染 description。
    """

    file_hash: str
    description: str
    saved_at: datetime
    context_note: str | None = None


# PendingReplyView 已于 2026-07-24 删除（待办#19），承载它的 reply / ReplyTask
# 体系整套已于 2026-08-17 删除（提案-裁决流水线取而代之）。TimelineItem 仍保留
# "reply_task_completed" 这个 kind：库里存量的 runtime.reply_task_completed 还要
# 兼容渲染一个版本周期，只是不会再有新的写入方。


@dataclass(frozen=True)
class ReflectionView:
    """最新一版自我认识（agent.reflection_written 折叠，latest-wins）。

    ``at`` 是写下它的时刻，与正文一起渲染——"三小时前想的"和"十分钟前想的"
    对这段认识还作不作数是两回事，只给正文等于抹掉这个判据。
    """

    at: datetime
    text: str


@dataclass(frozen=True)
class TimelineItem:
    """One renderable row in the LLM context (任务与决策契约 §2.1)."""

    event_id: str
    occurred_at: datetime
    kind: Literal[
        "message",
        "notice",
        "tool_call",
        "system_hint",
        "request",
        "task_closed",
        "my_reply",
        "reply_task_completed",
        "program",
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
    """Folded task state from agent.task_* events (任务与决策契约 §8)."""

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
    (任务与决策契约 §7.2、§11).

    成功/失败只靠内容区分：``error_kind is None`` 为成功（``result`` 有效），
    非 None 为失败（error_* 有效）。所属 decision 尚无 program terminal 且
    调用本身无 terminal 时，``error_kind`` 为 ``pending``，渲染成中性
    「已调用」。只有收口后的半截才是 ``interrupted`` / ``uncertain``。
    """

    tool_call_id: str
    tool_name: str
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
    # ─── 表情包收藏夹（meme_collection 管收藏；send_messages 发送）───
    # 全局共享的 agent_memes（2026-07-06 起全 bot 一份，created_at 倒序、
    # 封顶 meme_store.MAX_SAVED_MEMES 条），由 Projector.
    # _augment_with_saved_memes 注入；渲染成 `## 表情包收藏` 节，meme 工具凭
    # 其中的 hash 精确删除/换描述，并供发言时选图。空 = 不渲染。
    saved_memes: list[MemeView] = field(default_factory=list)
    # 2026-07-02 起不再有独立的 pending_tool_results 字段：工具结果只在
    # timeline 的终态 <工具> 行呈现一次（单一事实源）。
    # 旧的"待消费工具更新区"实现从未做过消费切割——窗口内所有结果每拍
    # 重复以"待你处理"的名义出现，是复读的直接诱饵；且同一调用在 timeline
    # 与 pending 区双重渲染，两处语义必然漂移。ToolResultView 仍保留——它是
    # timeline 渲染 tool-call 行时的折叠视图（fold_tool_results）。

    # ─── 程序源码进入时间线（2026-08-14）───
    # DecisionOutput.program 随 agent.decision_emitted 落库并渲染为 <程序>决策。
    # 它表示当拍产出了什么，不表示已经落地；落地看 <工具> 与 <程序>完成|失败。

    # ─── 自我认识（2026-08-03，reflect 工具）───
    # agent.reflection_written 折叠出的最新一版正文，latest-wins；渲染成信封
    # `## 反思` 一节。None / 空串 = 还没写过，整节不出现。
    #
    # 它是**第二个**跨拍连续装置（第一个是 active_tasks），两者分工不同：任务
    # 承载未竟之事、有收束条件；反思承载对自己的认识、没有终点，只被后来的
    # 版本整段改写。事件本身在 timeline 里消隐（build_timeline 跳过），避免
    # 同一段文字两处渲染。
    #
    # 与 2026-08-01 删除的 `<my-thought>` 逐拍 reasoning 回显的边界：那次删的是
    # **每拍自由笔记原样回到下一拍**（快照实证：变成写给自己的高显著度提示词、
    # 产出模板化台词）。这里回来的不是笔记而是一段被主动整合过的结论，且低频、
    # 全量替换、有字数上限——中间隔着一次整合，立场不会逐字继承。勿把本字段
    # 扩展成"最近 K 版反思"，那会退化回被删掉的那个形态。
    reflection: ReflectionView | None = None

    # ─── 已退役：同拍「校验拒绝」纠错环（2026-08-11）───
    # 字段保留以免旧快照/测试构造炸掉；AgentLoop 不再写入，LLMPlanner 若收到
    # 非 None 仍可渲染，但生产路径恒为 None。
    validation_feedback: ProgramValidationFeedback | None = None

    # 当前 tick 上 bot 自己的 QQ user_id（由 bot_registry 提供,AgentLoop
    # 在 tick() 时 resolve 后注入）。None 表示 bot 还没连接 napcat / 注册
    # 第一条事件 —— prompt 渲染时不输出该属性，模型回退到"靠引用反推"。
    bot_user_id: str | None = None

    # 当前 tick 在该 group scope 下小奏自己的群角色（owner / admin / member）。
    # 由 Projector.fold_bot_role() 从 runtime.bot_role_observed 事件折出最新值，
    # AgentLoop 在 dispatch 时原样注入 tool_called.payload.bot_role。它有两个用途：
    # ① 渲染成信封头部第二行 ``群角色 <role>`` 供 LLM 判断能不能调需要角色的工具；
    # ② 作为工具内 enforce_bot_admin 的**回退快照**——真正判权限时工具会先
    #    **实时**向 napcat 查 bot 当前角色（_effective_bot_role），查不到才回退到它。
    # None = 未观测到（启动初期 sweep 未跑完 / 该群从未写过 baseline）——渲染时不输出
    # 该属性；工具侧若实时查也拿不到，则保守拒绝带 required_bot_role 的调用。
    bot_role: Literal["owner", "admin", "member"] | None = None


class Planner(Protocol):
    """Stateless decision function.

    Implementations:
    - LLMPlanner — 现役唯一实现；每次调用模型一次并返回响应源码。
    - report_invalid_output — 静态预检失败后同步回报路由层，冷却当前端点，
      同拍下一次 decide 换组内下一个模型（不喂校验拒绝文本）。
    """

    async def decide(self, context: DecisionContext) -> DecisionOutput: ...

    def report_invalid_output(self, reason: str) -> None: ...

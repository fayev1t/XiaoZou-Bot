"""agent_tasks 读模型表 —— 任务状态的持久投影（CQRS read model）。

为什么存在（开发文档/v2.0/③状态折叠与投影 §6.1 选项A）：
  任务的"真相源"始终是 agent_events 里的 agent.task_* 事件流（append-only）。
  但 Projector.fold_tasks 只在最近 300 条 / 24h 的窗口里折叠 active_tasks，
  水群 / 跨天会把未完成任务的 agent.task_created 挤出窗口 —— 之后这个任务从
  active_tasks 凭空消失，且其后续 task_state_changed 因 "task_created 没折到"
  被一并丢弃（projection.fold_tasks）。这与 README "任务跨 tick 持久存在" 的
  契约冲突，是 bug。

  本表是从 task 事件派生的读模型：每写一条 agent.task_* 事件，事件落定**之后**
  best-effort 地 upsert 这张表（services/agent_loop/task_store.apply_task_event_safe，
  独立事务、失败只 log）。Projector 读未完成任务时直接查这张表（不受窗口限制），
  未完成任务因此永不丢。

与 append-only 硬规矩的关系：
  agent_events 仍是唯一真相源、仍禁止 UPDATE/DELETE。本表是【派生读模型】，
  允许 UPDATE —— 它随时能从事件流 replay 重建（task_store.backfill_recent）。
  **刻意不与事件写同事务**：派生视图的失败绝不能反过来拖垮 append-only 事件流的
  持久性（同事务下 upsert 失败会 abort 整个 PG 事务、连带丢掉本该落定的 task
  事件）。因此采用最终一致 + 自愈：短暂漂移由启动 `backfill_recent` 回填修正，
  且 Projector 以"窗口内折叠优先、表只补窗口外缺口"兜底——刚写的任务必在窗口里、
  由 fold_tasks 直接折出，根本不依赖表（见 projection.build_context）。

字段对齐 decision.TaskView。注意 **pending_tool_call_ids 不在表里维护** ——
在途工具调用天然是近期事件，由 Projector 的窗口折叠负责，读模型只管"任务还
在不在、是什么状态"这类跨窗口持久信息。
"""

from sqlalchemy import Column, DateTime, Index, Text
from sqlalchemy.dialects.postgresql import JSONB

from qqbot.models.base import Base


class AgentTask(Base):
    __tablename__ = "agent_tasks"

    task_id = Column(Text, primary_key=True)
    scope_key = Column(Text, nullable=False)
    description = Column(Text, nullable=False, default="")
    # list[str]
    related_tools = Column(JSONB, nullable=False, default=list)
    parent_task_id = Column(Text, nullable=True)
    # pending / running / done / failed
    state = Column(Text, nullable=False, default="pending")
    created_at = Column(DateTime(timezone=True), nullable=False)
    last_changed_at = Column(DateTime(timezone=True), nullable=False)
    last_change_reason = Column(Text, nullable=True)
    # search_history 的锚点：创建该任务的那条事件（任务与决策契约 §动态记忆检索）
    triggered_by_event_id = Column(Text, nullable=True)
    # list[{"at": iso8601, "note": str}]；append-only 累积，读出时取尾部 N 条
    # （与 Projector.MAX_PROGRESS_NOTES_PER_TASK 对齐，见 task_store._row_to_task_view）
    progress_notes = Column(JSONB, nullable=False, default=list)

    __table_args__ = (
        # 热路径：按 scope 取未完成任务（task_store.load_active_tasks）
        Index("agent_tasks_scope_state_idx", "scope_key", "state"),
    )

    def __repr__(self) -> str:
        return f"<AgentTask({self.task_id} {self.state} scope={self.scope_key})>"

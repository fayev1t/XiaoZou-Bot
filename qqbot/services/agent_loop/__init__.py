"""AgentLoop: v2 decision/tick layer.

Contracts:
- 开发文档/v2.0/任务与决策契约.md (DecisionRound, Action, Planner)
- 开发文档/v2.0/事件系统设计.md §10 (实例化策略)
- 开发文档/v2.0/EventIngest契约.md §5 (LoopSupervisor.wake interface)

Skeleton scope (当前阶段):
- AgentLoop tick 走完整路径：聚合 stub → planner → action 翻译 → 事件落表。
- 投影器尚未接入，DecisionContext 为最小骨架。
- FakeIdlePlanner 永远输出 idle，验证管道串通。
- 仅写 runtime.tick_started / agent.idle_decision / runtime.tick_ended 三类事件。
- 私聊 (scope=private) 不实例化 loop (任务与决策契约 §10.1)。
"""

from qqbot.services.agent_loop import bot_registry
from qqbot.services.agent_loop.decision import (
    Action,
    CallToolAction,
    CompleteTaskAction,
    CreateTaskAction,
    DecisionContext,
    DecisionOutput,
    FailTaskAction,
    IdleAction,
    ImageRef,
    NoteTaskProgressAction,
    Planner,
    ProgressNote,
    TaskView,
    TimelineItem,
    ToolResultView,
)
from qqbot.services.agent_loop.llm_planner import (
    LLMPlanner,
    build_default_prompt_registry,
)
from qqbot.services.agent_loop.loop import AgentLoop
from qqbot.services.agent_loop.planner import FakeIdlePlanner
from qqbot.services.agent_loop.projection import Projector
from qqbot.services.agent_loop.prompt_registry import PromptRegistry
from qqbot.services.agent_loop.reply_worker import ReplySendWorker
from qqbot.services.agent_loop.supervisor import LoopSupervisor
from qqbot.services.agent_loop.tool_registry import Tool, ToolRegistry
from qqbot.services.agent_loop.tool_worker import ToolWorker

__all__ = [
    "Action",
    "AgentLoop",
    "CallToolAction",
    "CompleteTaskAction",
    "CreateTaskAction",
    "DecisionContext",
    "DecisionOutput",
    "FailTaskAction",
    "FakeIdlePlanner",
    "IdleAction",
    "ImageRef",
    "LLMPlanner",
    "LoopSupervisor",
    "NoteTaskProgressAction",
    "Planner",
    "PromptRegistry",
    "build_default_prompt_registry",
    "ProgressNote",
    "Projector",
    "ReplySendWorker",
    "TaskView",
    "TimelineItem",
    "Tool",
    "ToolRegistry",
    "ToolResultView",
    "ToolWorker",
    "bot_registry",
]

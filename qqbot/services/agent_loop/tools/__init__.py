"""V2 内置工具集中注册。

每个文件实现一个 Tool（满足 qqbot.services.agent_loop.tool_registry.Tool
协议）。`build_default_registry(session_factory, wake_reply_worker=None)`
把所有内置工具注册到一个新的 ToolRegistry 实例返回，plugin 启动时调用
并注入 LoopSupervisor / LLMPlanner。

session_factory 是必填项：所有工具都通过 EventWriter 写事件 / 部分工具
（search_history）查 agent_events 表。

wake_reply_worker 是可选回调：ReplyTool 写完 agent.reply_emitted 后调用
它唤醒 ReplySendWorker 立即出货；None 时退化为不主动唤醒（worker 启动期
catchup 兜底，仅延迟变长）。生产路径由 v2_main 注入
supervisor.notify_reply_pending；测试路径常省略。

不复用 v1 qqbot/services/web_search.py 等业务实现 —— v2 工具从零写。
"""

from __future__ import annotations

from typing import Callable

from sqlalchemy.ext.asyncio import AsyncSession

from qqbot.services.agent_loop.tool_registry import ToolRegistry
from qqbot.services.agent_loop.tools.reply import ReplyTool
from qqbot.services.agent_loop.tools.search_history import SearchHistoryTool
from qqbot.services.agent_loop.tools.websearch import WebsearchTool

SessionFactory = Callable[[], AsyncSession]
WakeCallback = Callable[[], None]


def build_default_registry(
    session_factory: SessionFactory,
    *,
    wake_reply_worker: WakeCallback | None = None,
) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(WebsearchTool())
    registry.register(SearchHistoryTool(session_factory=session_factory))
    registry.register(
        ReplyTool(
            session_factory=session_factory,
            wake_reply_worker=wake_reply_worker,
        )
    )
    return registry


__all__ = [
    "build_default_registry",
    "ReplyTool",
    "SearchHistoryTool",
    "WebsearchTool",
]

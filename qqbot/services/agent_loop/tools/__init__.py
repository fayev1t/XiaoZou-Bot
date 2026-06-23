"""V2 内置工具集中注册。

每个文件实现一个 Tool（满足 qqbot.services.agent_loop.tool_registry.Tool
协议，继承 BaseTool 拿默认属性）。`build_default_registry()` 把所有内置
工具无参注册到一个新的 ToolRegistry 实例返回，plugin 启动时调用并注入
LoopSupervisor / LLMPlanner。

工具不再有构造依赖：系统级依赖（session_factory 写/查 agent_events、
notify_reply_pending 唤醒 ReplySendWorker）一律由 ToolWorker 在 run() 的
context 里统一注入。这样新增工具只要 register 一行，系统也不必按名字
特判任何工具（如旧的 reply.set_wake_callback 回填已删除）。

不复用 v1 qqbot/services/web_search.py 等业务实现 —— v2 工具从零写。
"""

from __future__ import annotations

from qqbot.services.agent_loop.tool_registry import ToolRegistry
from qqbot.services.agent_loop.tools.reply import ReplyTool
from qqbot.services.agent_loop.tools.search_history import SearchHistoryTool
from qqbot.services.agent_loop.tools.websearch import WebsearchTool


def build_default_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(WebsearchTool())
    registry.register(SearchHistoryTool())
    registry.register(ReplyTool())
    return registry


__all__ = [
    "build_default_registry",
    "ReplyTool",
    "SearchHistoryTool",
    "WebsearchTool",
]

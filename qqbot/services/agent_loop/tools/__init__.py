"""V2 内置工具集中注册。

每个文件实现一个 Tool（满足 qqbot.services.agent_loop.tool_registry.Tool
协议）。`build_default_registry(session_factory)` 把所有内置工具注册到一个新的
ToolRegistry 实例返回，plugin 启动时调用并注入 LoopSupervisor / LLMPlanner。

session_factory 是必填项：部分工具（如 search_history）需要查 agent_events
表。无 DB 需求的工具忽略它即可。

不复用 v1 qqbot/services/web_search.py 等业务实现 —— v2 工具从零写。
"""

from __future__ import annotations

from typing import Callable

from sqlalchemy.ext.asyncio import AsyncSession

from qqbot.services.agent_loop.tool_registry import ToolRegistry
from qqbot.services.agent_loop.tools.search_history import SearchHistoryTool
from qqbot.services.agent_loop.tools.websearch import WebsearchTool

SessionFactory = Callable[[], AsyncSession]


def build_default_registry(session_factory: SessionFactory) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(WebsearchTool())
    registry.register(SearchHistoryTool(session_factory=session_factory))
    return registry


__all__ = ["build_default_registry", "SearchHistoryTool", "WebsearchTool"]

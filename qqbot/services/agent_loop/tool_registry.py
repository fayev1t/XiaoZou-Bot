"""Tool 协议与 registry。

一个 Tool 描述了 agent 可调用的一项能力：
  - name              工具名（agent.tool_called.payload.tool_name 匹配它）
  - description       提示给 LLM 的简短说明（写入 prompt 的 tool_catalog）
  - arguments_schema  JSON Schema (dict)，描述 arguments 字段，纯文档用途
  - run(arguments)    异步执行；返回 dict（写入 agent.tool_result.payload.result）

执行约定（任务与决策契约 §5.1, §6）：
  - 成功：返回 JSON-serializable dict
  - 失败：raise 任何异常；ToolWorker 捕获后写 agent.tool_failed(error_kind, error_message)
  - 不强制 arguments 校验；轻量 tool 直接消费 dict 即可，需要严格校验可在 run() 内自行做
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Tool(Protocol):
    name: str
    description: str
    arguments_schema: dict

    # `usage_prompt` 不是必填——老工具或单测里的 stub 可以省略。
    # ToolRegistry.usage_docs() 用 getattr 兜底，缺失等同于空串。
    # 命名约定：把详细用法（何时该调、参数搭配、结果解读、踩坑）放在
    # sibling .md，由 prompts.load_sibling_md(__file__, "...") 加载注入。

    async def run(self, arguments: dict, **context: Any) -> Any:
        """Run the tool.

        `arguments` is the LLM-supplied dict matching `arguments_schema`.
        `context` carries system-injected kwargs (scope_key, task_id, ...) —
        tools that don't need them should accept **_ to ignore.
        """
        ...


class ToolRegistry:
    """简单的 name → Tool 字典。后续如需作用域/scope 隔离再扩。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        name = getattr(tool, "name", None)
        if not name or not isinstance(name, str):
            raise ValueError("tool.name must be a non-empty string")
        if name in self._tools:
            raise ValueError(f"tool already registered: {name}")
        self._tools[name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools.keys())

    def catalog(self) -> list[dict]:
        """渲染给 LLM 的工具清单。LLMPlanner 把这个塞进 prompt。"""
        return [
            {
                "name": t.name,
                "description": t.description,
                "arguments_schema": t.arguments_schema,
            }
            for t in self._tools.values()
        ]

    def usage_docs(self) -> str:
        """汇总所有已注册工具的 usage_prompt，PromptRegistry 在 system
        prompt 里作为一段注入。空 usage_prompt 的工具静默跳过 —— 不会出
        现孤儿 `## Tool: foo` 标题。
        """
        sections: list[str] = []
        for name in self.names():
            tool = self._tools[name]
            usage = getattr(tool, "usage_prompt", "") or ""
            usage = str(usage).strip()
            if not usage:
                continue
            sections.append(f"## Tool: {name}\n\n{usage}")
        return "\n\n".join(sections)

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._tools

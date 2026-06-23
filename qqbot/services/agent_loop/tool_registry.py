"""Tool 协议与 registry。

一个 Tool 描述了 agent 可调用的一项能力：
  - name                 工具名（agent.tool_called.payload.tool_name 匹配它）
  - description          提示给 LLM 的简短说明（写入 prompt 的 tool_catalog）
  - arguments_schema     JSON Schema (dict)，描述 arguments 字段，纯文档用途
  - required_permission  (可选) 触发用户最低 tier；默认 GUEST
  - require_bot_admin    (可选) 是否要求小奏自己在群里是 admin/owner；默认 False
  - run(arguments)       异步执行；返回 dict（写入 agent.tool_result.payload.result）

权限语义（详见 core/permissions.py）：

- 这两个属性是**纯元数据**，不影响 catalog 可见性 —— LLM 总能看见所有工具，
  并通过 description 自行判断是否可调；真要硬调，AgentLoop 闸门拦下来写
  agent.tool_failed(permission_denied_*)，LLM 下个 tick 据此回复"我没权限"。
- ToolRegistry.catalog() 把这两个属性透传出来，让 LLM 拿到的 tool 描述里
  自带权限元数据。

执行约定（任务与决策契约 §5.1, §6）：
  - 成功：返回 JSON-serializable dict
  - 失败：raise 任何异常；ToolWorker 捕获后写 agent.tool_failed(error_kind, error_message)
  - 不强制 arguments 校验；轻量 tool 直接消费 dict 即可，需要严格校验可在 run() 内自行做
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from qqbot.core.permissions import PermissionTier


@runtime_checkable
class Tool(Protocol):
    name: str
    description: str
    arguments_schema: dict

    # `usage_prompt` 不是必填——老工具或单测里的 stub 可以省略。
    # ToolRegistry.usage_docs() 用 getattr 兜底，缺失等同于空串。
    # 命名约定：把详细用法（何时该调、参数搭配、结果解读、踩坑）放在
    # sibling .md，由 prompts.load_sibling_md(__file__, "...") 加载注入。

    # `required_permission` / `require_bot_admin` 都不是必填——老工具或单测
    # stub 可以省略。ToolRegistry.catalog() / AgentLoop 闸门用 getattr 兜底，
    # 缺失等同于 GUEST / False。

    async def run(self, arguments: dict, **context: Any) -> Any:
        """Run the tool.

        `arguments` is the LLM-supplied dict matching `arguments_schema`.
        `context` carries system-injected kwargs (scope_key, task_id,
        correlation_id, session_factory, notify_reply_pending, ...) — tools
        that don't need them should accept **_ to ignore. ToolWorker injects
        the SAME context for every tool, so no tool needs bespoke
        construction wiring — see BaseTool.
        """
        ...


class BaseTool:
    """工具基类：把"可选属性"的默认值固化在一处，realize "系统只认输入输出"。

    继承它后，工具只需覆盖与默认不同的字段（几乎总是 name / description /
    arguments_schema / usage_prompt）；权限相关默认 GUEST / 非 bot-admin。
    需要敏感权限的工具显式覆盖 `required_permission` / `require_bot_admin`
    即可（如未来的踢人工具 = ADMIN + require_bot_admin）。

    系统级依赖（session_factory / notify_reply_pending 等）一律由 ToolWorker
    在 run() 的 context 里统一注入，不再走各工具自己的 __init__ —— 这样
    build_default_registry 能无参构造所有工具，系统也不必按名字特判任何工具。

    注：`get_tool_required_permission` / `get_tool_require_bot_admin` 仍保留为
    防御层 —— 测试 stub 或第三方工具不继承 BaseTool 时也能拿到默认值。
    """

    usage_prompt: str = ""
    required_permission: PermissionTier = PermissionTier.GUEST
    require_bot_admin: bool = False


def get_tool_required_permission(tool: Any) -> PermissionTier:
    """统一读 tool.required_permission 的兜底入口。

    缺失 → GUEST；字符串值（"ADMIN" / "admin"）按 enum name 解析；其它非法
    值 fall back 到 GUEST。集中实现避免每个 caller 都写重复 isinstance。
    """
    raw = getattr(tool, "required_permission", None)
    if raw is None:
        return PermissionTier.GUEST
    if isinstance(raw, PermissionTier):
        return raw
    if isinstance(raw, str):
        try:
            return PermissionTier[raw.strip().upper()]
        except KeyError:
            return PermissionTier.GUEST
    if isinstance(raw, int):
        try:
            return PermissionTier(raw)
        except ValueError:
            return PermissionTier.GUEST
    return PermissionTier.GUEST


def get_tool_require_bot_admin(tool: Any) -> bool:
    """统一读 tool.require_bot_admin 的兜底入口。缺失 → False。"""
    return bool(getattr(tool, "require_bot_admin", False))


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
        """渲染给 LLM 的工具清单。LLMPlanner 把这个塞进 prompt。

        权限元数据（required_permission / require_bot_admin）随每个条目透出，
        **不在这里做可见性过滤** —— LLM 始终看见全部工具，硬调失败由 AgentLoop
        闸门处理，让 LLM 能感知 "我没权限" 然后礼貌回复。
        """
        return [
            {
                "name": t.name,
                "description": t.description,
                "arguments_schema": t.arguments_schema,
                "required_permission": get_tool_required_permission(t).name,
                "require_bot_admin": get_tool_require_bot_admin(t),
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

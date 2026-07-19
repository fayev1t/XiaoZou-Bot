"""PromptRegistry — 把多源的提示词片段统一拼成最终 system prompt。

动机
----
v2 系统里需要往 LLM 的 system prompt 注入的内容会越来越多：

  - 机器身份（identity.md：决策引擎操作一个 QQ 账号）
  - 决策协议（protocol.md，原 _SYSTEM_PROMPT）
  - 参与规则（group_chat_rules.md：什么时候有理由落 reply_task）
  - 每个 tool 的用法说明（tools/<name>.md；角色卡由独立 Replyer 消费）
  - 未来还会有 task 模板、风控指南、运行期反射……

把这些散在 llm_planner 里硬拼会越来越乱。PromptRegistry 提供一个最小内核：

  - register(name, order, content) 注册一段；content 可以是字符串或
    返回字符串的 callable（callable 在 render 时才求值，便于"渲染时再
    遍历 ToolRegistry"这类懒求值场景）
  - render() 按 order 升序拼接，section 之间用 `\n\n---\n\n` 分隔；
    返回空串的 section 自动忽略，不会出现孤儿分隔符

设计取舍
--------
- 不做 section 间依赖：order 是唯一排序信号。需要"我在 X 之后"就显式
  给一个比 X 大的 order 数字。逻辑越简单越好。
- 不做模板插值：每个 section 的 content 自己负责生成最终文本，registry
  只管串起来。模板需求让调用方在 callable 里处理。
- name 只用于 debug / 覆盖（同 name 再 register 会覆盖前者，方便测试
  替换某一段）。
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Callable, Union


@dataclass(frozen=True)
class RenderedSection:
    """render_sections() 的单段产物：段名 + 求值后的正文（已 strip）。

    给 Prompt 快照（prompt_snapshot.py，待办 #11）统计"system prompt 每段
    占多少"用——快照层自己算 chars / sha256，registry 只负责把段名和正文
    成对交出去，保持零依赖。
    """

    name: str
    text: str

# source 可以是纯字符串、无参 ``() -> str``、或接受一个 scope 位置参的
# ``(scope) -> str``（如 ToolRegistry.usage_docs）。render(scope=...) 会按 arity
# 自动决定是否把 scope 传进去——见 _resolve_source。
PromptSource = Union[str, Callable[..., str]]


@dataclass
class _Section:
    name: str
    order: int
    source: PromptSource


# 段间分隔符。公开导出：llm_planner 用 render_sections() 拿逐段结果后需要
# 用同一分隔符拼回完整 system prompt（与 render() 逐字节一致）。
SECTION_SEP = "\n\n---\n\n"
_SECTION_SEP = SECTION_SEP


class PromptRegistry:
    def __init__(self) -> None:
        self._sections: dict[str, _Section] = {}

    def register(
        self,
        name: str,
        order: int,
        content: PromptSource,
    ) -> None:
        """注册或覆盖一段 prompt。

        - name: 唯一标识，调试和单测用；同名再 register 直接覆盖
        - order: 排序键，render 时升序拼接；约定区段：
            0-49    机器身份（identity）
            50-99   输入格式（xml_format）
            100-149 参与规则（group_chat_rules）
            150-299 决策协议（protocol）
            300+    工具用法（每个工具一段，order 内部相对随意）
        - content: 字符串或 () -> str；后者在 render 时才求值
        """
        if not name:
            raise ValueError("section name required")
        self._sections[name] = _Section(name=name, order=order, source=content)

    def remove(self, name: str) -> None:
        self._sections.pop(name, None)

    def has(self, name: str) -> bool:
        return name in self._sections

    def render(self, *, scope: str | None = None) -> str:
        """求值所有 section、按 order 升序拼出最终 system prompt。

        ``scope``（"group"/"private"/"system"）用于 **per-scope 过滤**：接受一个位置
        参的 section source（如 ``ToolRegistry.usage_docs``）会收到它，据此只渲染当前
        scope 可见的工具用法——群专用工具的用法文档不会泄漏进 system loop 的 prompt，
        反之亦然（与 ``catalog(scope)`` 对齐，契约 §2.2）。无参 source（人设 / 协议等）
        照常无参调用。``scope=None``（默认）= 不过滤，兼容旧调用。

        空 section（求值后 strip 为 ""）忽略；不会产生 `---\n\n---` 这种相邻分隔符。
        callable 抛异常时该段静默丢弃 —— 启动期 prompt 不应阻断整个 tick，丢失的内容
        由日志暴露即可。
        """
        return _SECTION_SEP.join(
            sec.text for sec in self.render_sections(scope=scope)
        )

    def render_sections(
        self, *, scope: str | None = None
    ) -> list[RenderedSection]:
        """与 render() 同一求值过程，但保留段边界返回逐段结果。

        供 Prompt 快照统计每段体积（待办 #11）。语义与 render() 严格一致：
        同一 order 升序、同一 scope 传递规则、空段与抛异常段同样丢弃——
        ``render()`` 就是本方法的 join，两者永不发散。
        """
        from qqbot.core.logging import get_logger

        logger = get_logger(__name__)

        ordered = sorted(self._sections.values(), key=lambda s: (s.order, s.name))
        rendered: list[RenderedSection] = []
        for sec in ordered:
            try:
                text = _resolve_source(sec.source, scope)
            except Exception as exc:
                logger.warning(
                    "[prompt_registry] section {!r} render failed: {}", sec.name, exc
                )
                continue
            if text is None:
                continue
            stripped = str(text).strip()
            if not stripped:
                continue
            rendered.append(RenderedSection(name=sec.name, text=stripped))
        return rendered

    def section_names(self) -> list[str]:
        """按 render 顺序返回 section 名，主要给单测 / debug 用。"""
        return [
            s.name
            for s in sorted(self._sections.values(), key=lambda s: (s.order, s.name))
        ]


def _resolve_source(source: PromptSource, scope: str | None) -> str:
    """求值一个 section source。字符串原样返回；callable 按 arity 调用：接受位置参
    的传 ``scope``（per-scope 过滤），无参的直接调用（向后兼容 ``() -> str``）。"""
    if not callable(source):
        return source
    if _accepts_positional_arg(source):
        return source(scope)
    return source()


def _accepts_positional_arg(fn: Callable[..., str]) -> bool:
    """fn 是否接受至少一个位置参数（用来接收 scope）。无法内省（内置 / C 实现）时
    保守当作"不接受"，按无参调用——绝不会因内省失败而误传参把老 source 打挂。"""
    try:
        sig = inspect.signature(fn)
    except (ValueError, TypeError):
        return False
    for p in sig.parameters.values():
        if p.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.VAR_POSITIONAL,
        ):
            return True
    return False

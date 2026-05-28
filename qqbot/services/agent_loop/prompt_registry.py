"""PromptRegistry — 把多源的提示词片段统一拼成最终 system prompt。

动机
----
v2 系统里需要往 LLM 的 system prompt 注入的内容会越来越多：

  - 人设（persona.md）
  - 决策协议（protocol.md，原 _SYSTEM_PROMPT）
  - reply 段的可用 segment 文档（reply.md）
  - 每个 tool 的用法说明（tools/<name>.md）
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

from dataclasses import dataclass
from typing import Callable, Union

PromptSource = Union[str, Callable[[], str]]


@dataclass
class _Section:
    name: str
    order: int
    source: PromptSource


_SECTION_SEP = "\n\n---\n\n"


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
            0-99    人设
            100-199 决策协议
            200-299 输出动作文档（reply / 状态机动作等）
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

    def render(self) -> str:
        """求值所有 section、按 order 升序拼出最终 system prompt。

        空 section（求值后 strip 为 ""）忽略；不会产生 `---\n\n---` 这种
        相邻分隔符。callable 抛异常时该段静默丢弃 —— 启动期 prompt 不应
        阻断整个 tick，丢失的内容由日志暴露即可。
        """
        from qqbot.core.logging import get_logger

        logger = get_logger(__name__)

        ordered = sorted(self._sections.values(), key=lambda s: (s.order, s.name))
        parts: list[str] = []
        for sec in ordered:
            try:
                text = sec.source() if callable(sec.source) else sec.source
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
            parts.append(stripped)
        return _SECTION_SEP.join(parts)

    def section_names(self) -> list[str]:
        """按 render 顺序返回 section 名，主要给单测 / debug 用。"""
        return [
            s.name
            for s in sorted(self._sections.values(), key=lambda s: (s.order, s.name))
        ]

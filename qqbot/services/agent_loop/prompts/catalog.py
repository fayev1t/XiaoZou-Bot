"""提示词库 — 每个消费者一张根页，共享资产由页内 `{{槽}}` 拼进来。

系统里五个 LLM 调用点（Planner / meme caption / image description /
look_at_image / 记忆压缩）的 system prompt 都从这里装配。**只有一种装配机制**：
`CONSUMERS` 把消费者映射到它的根页 `.md`，根页正文里写 `{{name}}` 就把对应资产
拼在那个位置。改提示词 = 改 `prompts/` 下的 `.md`（render 时才读盘，改完即生效，
无需重启）；给新消费者配 prompt = 加一张根页 + 在 `CONSUMERS` 里登记。

**2026-07-30 统一为槽**：此前是两套机制并存——`ASSEMBLY` 列出段名按序拼接，
外加一个 `PERSONA_SLOT` 专供角色卡就地替换。同一件事两种做法，且顺序（在
`ASSEMBLY` 里）与框定（在 `.md` 里）分居两处，读页的人看不出隔壁那句话最后会落
在哪一段旁边。现在位置、顺序、分隔符全部由根页自己写死，看得见即所得。
`ASSEMBLY` 与 `SECTION_SEP` 一并删除——段间那行 `---` 现在是各页正文里的字符。

**2026-07-31 删除 Replyer**（重构提案-删除Replyer.md）：`replyer.md` 根页随之
删除，Planner 独自承担分析与最终措辞。角色卡回到单一消费者，`planner.md` 把
`{{persona}}` 放在页首——那就是她自己，旧的「这个qq号背后的人格是…」第三方
框定引导语随分工一并退役，**不要再加回来**（那句话存在的唯一理由是把卡片框成
对下游角色的描述，而下游角色已不存在）。

**本文件同时是提示词资产的地图——维护注记写在这里，不写进 `.md`（那些文件逐
字节注入 prompt，放不了给人看的注释）。**

资产分工（一个文件一个主题）：

  共享资产 —— 谁需要谁在自己的根页里开槽：
  - `persona.md`  角色卡本体：她是谁、什么性格、什么行为倾向、说话什么标准。
                  第二人称写成；`planner.md` 把槽放在最前（那就是她自己）。
  - `system.md`   这台机器怎么转：按拍运行、时间线是唯一真相源。仍保持
                  **客观语域、不点名任何独立角色**——它描述的是运行事实，
                  不该随分工变化重写。
  - `envelope.md` 输入信封语法（`<agent-input>` 的唯一出处）。
  - `group_chat_rules.md` 参与判断（把 timeline 当整体语境读、自主判断是否行动）。
  - `tools_usage` 唯一的动态槽：render 时遍历 ToolRegistry，按 scope 过滤。求值
                  为空时**连同它独占的那一行一起消失**，不留空洞。

  根页 —— 只放这个调用点独有的东西：
  - `planner.md`  人格槽、这一环的三条纪律（念头≠动作 / 跨拍靠任务 / 一批工具
                  不要重拨）、两步发言流程（reply 等待 → completed 唤醒 →
                  send_messages 落笔）、决策 JSON 输出契约。
  - 另外四个小消费者一页一个 `.md`，页内无槽。
  - `tools/<name>.md` 逐工具用法，经 `{{tools_usage}}` 只进 Planner。

  **硬规则：根页之间永不互相开槽。** 加新 worker = 一张根页 + 它需要哪几个槽。

装配为什么这么分（历史故障，改动前先读）：
  - **角色卡只有一份真相源（`persona.md`）。** 历史上 `planner.md` 里存过一份
    与卡片性格段逐字节相同、只差人称的第三人称投影，是不折不扣的第二份副本；
    删除 Replyer 后卡片直接进 Planner 页首，副本问题不复存在——不要再往任何
    别的文件抄人格正文。
  - **纯记录/观察层（caption / image_description / image_look / memory）只读
    自己那一页。** 它们的输出会被永久写进事件正文并被下游反复读取，掺进人格
    或群规就等于污染所有下游语境，且无法回收。
  以上几条 2026-07-30 之前由 `kind` + `_FORBIDDEN_KINDS` + `_validate_assembly`
  做 build 期结构校验。已删除：它的粒度是"哪个文件进哪个消费者"，而真实发生过
  的事故是"人格正文被抄进另一个文件"，那种它一声不响。守这几条现在靠上面这段
  说明 + 契约测试里的语义断言（`test_prompt_catalog_contract.LayerBoundaryTests`：
  锚点在运行时从 persona.md / group_chat_rules.md 现取，锚点没了会先失败而不是
  假通过）——那才是唯一抓得到内容漂移的手段。

不变量：
  - 根页或文件槽读出来是空的 = 部署坏了，直接 raise `PromptSectionMissing`，
    绝不静默拿残缺 system prompt 继续跑。角色卡为空同样走这条（Planner 的
    prompt 装配失败 → 该拍降级 idle，见 llm_planner 的兜底）。
  - 未知槽名（写错、资产改名）同样 raise：静默留下一个 `{{typo}}` 字面量会直接
    出现在模型眼前，比缺一整段更难发现。
  - 动态槽（只剩 `tools_usage`）求值为空时跳过：未注入工具注册表的场景
    （早期骨架 / 部分测试）本就不该有工具用法段。
"""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Union

_PROMPTS_DIR = Path(__file__).parent

# source 可以是纯字符串、无参 ``() -> str``、或接受一个 scope 位置参的
# ``(scope) -> str``（如 ToolRegistry.usage_docs）。按 arity 决定是否传 scope。
PromptSource = Union[str, Callable[..., str]]

# 槽语法：``{{name}}``，名字限小写字母与下划线——正文里大量出现的 JSON 片段
# （`{"type":"text","data":{...}}`）因此不会被误当成槽。约定各页把槽单独写一行，
# 但**替换只吃 `{{…}}` 本身与同行左右的空格**，不碰换行：页里写了几个空行，
# 展开后就还是几个空行，分隔完全由页正文说了算。
SLOT_PATTERN = re.compile(r"\{\{([a-z_]+)\}\}")
_SLOT = re.compile(r"[ \t]*\{\{([a-z_]+)\}\}[ \t]*")
# 动态槽求值为空时，连它前面那条分隔线一起收掉（见 render_sections）。
_TRAILING_RULE = re.compile(r"\n[ \t]*-{3,}[ \t]*\n\s*\Z")


class PromptSectionMissing(RuntimeError):
    """根页、文件槽缺失或为空，或槽名未登记：部署损坏，fail loudly。"""


@dataclass(frozen=True)
class Section:
    """渲染产物的一段：名字 + 正文。

    `render_sections` 把根页按槽切开，literal 片段挂消费者名、槽片段挂槽名，
    **顺序拼起来逐字节等于 `render()`**（片段之间没有额外分隔符——分隔符是各页
    正文里的字符）。快照用它统计每部分体积。
    """

    name: str
    text: str


# ── 槽名 → 文件名。任何根页都可以 {{name}} 引用 ──
_FILES: dict[str, str] = {
    "persona": "persona.md",
    "system": "system.md",
    "envelope": "envelope.md",
    "group_chat_rules": "group_chat_rules.md",
}

# ── 消费者 → 根页文件名。根页不是槽，不能被别的页引用 ──
CONSUMERS: dict[str, str] = {
    "planner": "planner.md",
    "caption": "meme_caption.md",
    "image_description": "image_description.md",
    "image_look": "image_look.md",
    "memory": "memory_compaction.md",
}

# ── 动态槽名：不读盘，由注入的 source 求值；求值为空则整行跳过 ──
DYNAMIC_SLOTS = ("tools_usage",)


class PromptLibrary:
    """一张根页 + 它可用的槽，render 时展开。

    槽的来源可以是字符串字面量（测试注入）或 callable（读盘 / 遍历工具注册表，
    render 时才求值，因此改 `.md` 立即生效）。
    """

    def __init__(
        self,
        page: PromptSource,
        slots: dict[str, PromptSource] | None = None,
        *,
        name: str = "page",
    ) -> None:
        self._page = page
        self._slots: dict[str, PromptSource] = dict(slots or {})
        self._name = name

    def add(self, name: str, source: PromptSource) -> None:
        """登记/替换一个槽（方便测试替换某一段）。"""
        if not name:
            raise ValueError("slot name required")
        self._slots[name] = source

    def remove(self, name: str) -> None:
        self._slots.pop(name, None)

    def has(self, name: str) -> bool:
        return name in self._slots

    def slot_names(self) -> list[str]:
        """本页正文里实际出现的槽名，按出现顺序。"""
        return [m.group(1) for m in SLOT_PATTERN.finditer(self._page_text())]

    def section_names(self) -> list[str]:
        """渲染产物各部分的名字，按顺序（literal 片段用消费者名）。"""
        return [sec.name for sec in self.render_sections()]

    def get(self, name: str, *, scope: str | None = None) -> str:
        """按名字取一个槽的正文。槽未登记时 KeyError。"""
        if name not in self._slots:
            raise KeyError(name)
        return str(_resolve(self._slots[name], scope) or "").strip()

    def render_sections(self, *, scope: str | None = None) -> list[Section]:
        """把根页按槽切成有名字的片段，顺序即拼接顺序。

        literal 片段挂消费者名，槽片段挂槽名；**顺序拼起来逐字节等于
        `render()`**（首尾已 trim，片段间没有额外分隔符）。文件槽为空即 raise，
        动态槽为空则整行跳过——两条规则见模块 docstring。
        """
        page = self._page_text()
        out: list[Section] = []
        cursor = 0
        for match in _SLOT.finditer(page):
            name = match.group(1)
            text = self._slot_text(name, scope)
            literal = page[cursor : match.start()]
            cursor = match.end()
            if text is None:
                # 动态槽求值为空：整行连同**紧挨它前面那条分隔线**一起消失，
                # 否则页尾会留下一条孤零零的 `---`。
                literal = _TRAILING_RULE.sub("", literal)
                if literal:
                    out.append(Section(name=self._name, text=literal))
                continue
            if literal:
                out.append(Section(name=self._name, text=literal))
            out.append(Section(name=name, text=text))
        tail = page[cursor:]
        if tail:
            out.append(Section(name=self._name, text=tail))
        return _trim_edges(out)

    def render(self, *, scope: str | None = None) -> str:
        """展开全部槽，拼出最终 system prompt。"""
        return "".join(sec.text for sec in self.render_sections(scope=scope))

    # ── 内部 ──

    def _page_text(self) -> str:
        text = str(_resolve(self._page, None) or "")
        if not text.strip():
            raise PromptSectionMissing(f"prompt page {self._name!r} is empty")
        return text

    def _slot_text(self, name: str, scope: str | None) -> str | None:
        """槽正文；动态槽求值为空返回 None（调用方据此整行跳过）。"""
        if name not in self._slots:
            raise PromptSectionMissing(
                f"unknown slot {{{{{name}}}}} in prompt page {self._name!r}; "
                f"known slots: {', '.join(sorted(self._slots)) or '(none)'}"
            )
        text = str(_resolve(self._slots[name], scope) or "").strip()
        if text:
            return text
        if name in DYNAMIC_SLOTS:
            return None
        raise PromptSectionMissing(
            f"prompt slot {name!r} ({_FILES.get(name, 'dynamic')}) is empty"
        )


def _trim_edges(sections: list[Section]) -> list[Section]:
    """掐掉首尾片段的边缘空白，让 `"".join(片段)` 直接等于最终 prompt——
    快照的分段统计与真正送进模型的字节因此不会差一个换行。"""
    while sections:
        first = sections[0]
        trimmed = first.text.lstrip()
        if trimmed:
            sections[0] = Section(name=first.name, text=trimmed)
            break
        sections.pop(0)
    while sections:
        last = sections[-1]
        trimmed = last.text.rstrip()
        if trimmed:
            sections[-1] = Section(name=last.name, text=trimmed)
            break
        sections.pop()
    return sections


def _file_source(filename: str) -> Callable[[], str]:
    """文件槽的懒加载 source：render 时读盘（热更新）。读盘异常原样上抛。"""

    def load() -> str:
        return (_PROMPTS_DIR / filename).read_text(encoding="utf-8")

    return load


def build_library(
    consumer: str,
    *,
    tool_registry: Any | None = None,
) -> PromptLibrary:
    """取某个消费者的根页 + 全部可用槽。未登记的消费者 KeyError。

    槽是**全部登记、按需使用**：页里没写 `{{group_chat_rules}}` 它就不出现，
    不需要在别处再声明一次要哪几段。`tools_usage` 只在传入 tool_registry 时
    登记；页里写了它而没传注册表，按动态槽规则整行跳过。
    """
    slots: dict[str, PromptSource] = {
        name: _file_source(filename) for name, filename in _FILES.items()
    }
    if tool_registry is not None:
        slots["tools_usage"] = tool_registry.usage_docs
    else:
        slots["tools_usage"] = ""
    return PromptLibrary(
        _file_source(CONSUMERS[consumer]), slots, name=consumer
    )


def render_system_prompt(
    consumer: str,
    *,
    scope: str | None = None,
    tool_registry: Any | None = None,
) -> str:
    """一步到位：取页 + 展开槽。Replyer / caption 这类单消费者调用点用。"""
    return build_library(consumer, tool_registry=tool_registry).render(
        scope=scope
    )


def _resolve(source: PromptSource, scope: str | None) -> str:
    """求值一个 source。字符串原样返回；callable 按 arity 调用：接受位置参的
    传 scope（如 ToolRegistry.usage_docs 按 scope 过滤工具），无参的直接调用。"""
    if not callable(source):
        return source
    if _accepts_positional_arg(source):
        return source(scope)
    return source()


def _accepts_positional_arg(fn: Callable[..., str]) -> bool:
    """fn 是否接受至少一个位置参数（用来接收 scope）。无法内省（内置 / C 实现）
    时保守当作"不接受"，按无参调用——绝不因内省失败而误传参把老 source 打挂。"""
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

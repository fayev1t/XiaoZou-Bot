"""提示词库 — 持有全部提示词文本，按名字取。

系统里六个 LLM 调用点（Planner / Replyer / meme caption / image description /
look_at_image / 记忆压缩）的 system prompt 都从这里装配：`_FILES` 是"段名 →
文件名"的清单，`ASSEMBLY` 是"消费者 → 段名序列"，序列顺序就是拼接顺序。
改提示词 = 改 `prompts/` 下对应 `.md`（render 时才读盘，改完即生效，无需重启）；
给新消费者配 prompt = 在 `ASSEMBLY` 里列出它要哪几段。

**本文件同时是提示词资产的地图——维护注记写在这里，不写进 `.md`（那些文件逐
字节注入 prompt，放不了给人看的注释）。**

资产分工（2026-07-30 重排：一个消费者一个职责页 + 一份共享世界文档）：
  - `planner.md`  Planner 的职责页：机器身份与第三人称弱人格投影、系统机制、
                  职责边界（为落笔那一环备料、不碰表达）、决策 JSON 输出契约。
  - `replyer.md`  Replyer 的职责页，也是角色卡本身（2026-07-30 由维护者把
                  `voice.md` 并入并删除后者）：她是谁、这一份要说什么的边界、
                  输出格式。整份只进 Replyer。
  - `envelope.md` 输入信封语法，**两个消费者共享同一份字节**：Replyer 的
                  `<replyer-input>` 里 timeline 由同一个 `render_timeline_stream`
                  渲染，与 Planner 逐字节同构；分成两份必然漂移。
  - `group_chat_rules.md` 参与判断（把 timeline 当整体语境读、自主判断是否行动）。
  - `tools/<name>.md` 逐工具用法，只进 Planner（Replyer 没有工具可调）。

装配单为什么这么分（历史故障，改动前先读）：
  - **整张角色卡不进 Planner。** 把 8KB 措辞/情绪/形态塞进一个输出 JSON 的
    决策层，`reasoning` 会开始用她的语气演戏，并拿心情当调工具的理由。Planner
    要的人格只是"她会不会理这件事"的窄投影，写在 `planner.md` 里。
  - **参与政策不进 Replyer。** Replyer 手上的稿子已经被授权了，让它重读一遍
    "要不要开口"，它会二次审查并产出空回复，架空 Planner 的决定。
  - **决策 JSON 契约与工具用法不进 Replyer。** 一个 prompt 两套输出规范必然
    串台（`{"actions":[…]}` 对 `{"messages":[…]}`）；工具用法对它是噪音加诱惑。
  - **纯记录/观察层（caption / image_description / image_look / memory）只读
    自己那一段。** 它们的输出会被永久写进事件正文并被下游反复读取，掺进人格
    或群规就等于污染所有下游语境，且无法回收。
  以上四条 2026-07-30 之前由 `kind` + `_FORBIDDEN_KINDS` + `_validate_assembly`
  做 build 期结构校验。已删除：它的粒度是"哪个文件进哪个消费者"，而真实发生过
  的事故是"人格正文被抄进另一个文件"，那种它一声不响；`ASSEMBLY` 本身就是六行
  字面量，改它的人正是知道这些规则的人。守这几条现在靠上面这段说明 + 契约测试
  里的语义断言（`test_prompt_catalog_contract.LayerBoundaryTests`：锚点在运行时
  从 replyer.md（角色卡现居所）/ group_chat_rules.md 现取，锚点没了会先失败而
  不是假通过）——那才是唯一抓得到内容漂移的手段。

不变量（不靠任何 per-section 元数据）：
  - 文件段读出来是空的 = 部署坏了，直接 raise `PromptSectionMissing`，绝不静默
    拿残缺 system prompt 继续跑。
  - 动态段（只剩 `tools_usage`）求值为空时跳过：未注入工具注册表的场景
    （早期骨架 / 部分测试）本就不该有工具用法段。

角色卡的 fail-loudly 由上面第一条统一兜住：`replyer.md` 是文件段，缺失即
read_text 抛 OSError、为空即 `PromptSectionMissing`，两者都被
`reply_executor._compose_and_send` 的兜底捕获并记成 `<my-reply status="failed">`。
2026-07-30 之前这条走 `replyer._load_voice_text()` 的 ReplyerError，随 voice.md
删除一并退役。
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence, Union

_PROMPTS_DIR = Path(__file__).parent

# 段间分隔符。公开导出：llm_planner 拿逐段结果后要用同一分隔符拼回完整
# system prompt（与 render() 逐字节一致）。
SECTION_SEP = "\n\n---\n\n"

# source 可以是纯字符串、无参 ``() -> str``、或接受一个 scope 位置参的
# ``(scope) -> str``（如 ToolRegistry.usage_docs）。按 arity 决定是否传 scope。
PromptSource = Union[str, Callable[..., str]]


class PromptSectionMissing(RuntimeError):
    """文件段缺失或为空：部署损坏，fail loudly。"""


@dataclass(frozen=True)
class Section:
    """求值后的单段产物：段名 + 正文（已 strip）。"""

    name: str
    text: str


# ── 段名 → 文件名。动态段（tools_usage）不在此表，见 build_library ──
_FILES: dict[str, str] = {
    "planner": "planner.md",
    "replyer": "replyer.md",
    "envelope": "envelope.md",
    "group_chat_rules": "group_chat_rules.md",
    "meme_caption": "meme_caption.md",
    "image_description": "image_description.md",
    "image_look": "image_look.md",
    "memory_compaction": "memory_compaction.md",
}

# ── 消费者 → 段名序列。列出顺序 = 拼接顺序 ──
ASSEMBLY: dict[str, tuple[str, ...]] = {
    "planner": ("planner", "envelope", "group_chat_rules", "tools_usage"),
    "replyer": ("replyer", "envelope"),
    "caption": ("meme_caption",),
    "image_description": ("image_description",),
    "image_look": ("image_look",),
    "memory": ("memory_compaction",),
}


class PromptLibrary:
    """一组按名字取用的提示词段，顺序即拼接顺序。

    段的来源可以是字符串字面量（测试注入）或 callable（读盘 / 遍历工具注册表，
    render 时才求值，因此改 `.md` 立即生效）。
    """

    def __init__(
        self, sections: Sequence[tuple[str, PromptSource]] | None = None
    ) -> None:
        self._sections: list[tuple[str, PromptSource]] = list(sections or ())

    def add(self, name: str, source: PromptSource) -> None:
        """追加一段；同名再 add 覆盖原位置的 source（方便测试替换某一段）。"""
        if not name:
            raise ValueError("section name required")
        for index, (existing, _) in enumerate(self._sections):
            if existing == name:
                self._sections[index] = (name, source)
                return
        self._sections.append((name, source))

    def remove(self, name: str) -> None:
        self._sections = [(n, s) for n, s in self._sections if n != name]

    def has(self, name: str) -> bool:
        return any(n == name for n, _ in self._sections)

    def section_names(self) -> list[str]:
        return [name for name, _ in self._sections]

    def get(self, name: str, *, scope: str | None = None) -> str:
        """按名字取一段的正文。段不存在时 KeyError。"""
        for existing, source in self._sections:
            if existing == name:
                return str(_resolve(source, scope) or "").strip()
        raise KeyError(name)

    def render_sections(self, *, scope: str | None = None) -> list[Section]:
        """逐段求值，保留段边界（供 Prompt 快照统计每段体积）。

        文件段为空即 raise；动态段为空跳过——两条规则见模块 docstring。
        """
        out: list[Section] = []
        for name, source in self._sections:
            text = str(_resolve(source, scope) or "").strip()
            if not text:
                if name in _FILES:
                    raise PromptSectionMissing(
                        f"prompt section {name!r} ({_FILES[name]}) is empty"
                    )
                continue
            out.append(Section(name=name, text=text))
        return out

    def render(self, *, scope: str | None = None) -> str:
        """按顺序拼出最终 system prompt。"""
        return SECTION_SEP.join(
            sec.text for sec in self.render_sections(scope=scope)
        )


def _file_source(filename: str) -> Callable[[], str]:
    """文件段的懒加载 source：render 时读盘（热更新）。读盘异常原样上抛。"""

    def load() -> str:
        return (_PROMPTS_DIR / filename).read_text(encoding="utf-8")

    return load


def build_library(
    consumer: str,
    *,
    tool_registry: Any | None = None,
) -> PromptLibrary:
    """按 ASSEMBLY 组装某个消费者的提示词库。未登记的消费者 KeyError。

    `tools_usage` 只在传入 tool_registry 时加入（未注入的场景不渲染工具用法段）。
    """
    library = PromptLibrary()
    for name in ASSEMBLY[consumer]:
        if name == "tools_usage":
            if tool_registry is not None:
                library.add(name, tool_registry.usage_docs)
        else:
            library.add(name, _file_source(_FILES[name]))
    return library


def render_system_prompt(
    consumer: str,
    *,
    scope: str | None = None,
    tool_registry: Any | None = None,
) -> str:
    """一步到位：组装 + 拼接。Replyer / caption 这类单消费者调用点用。"""
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

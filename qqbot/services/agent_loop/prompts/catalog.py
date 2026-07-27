"""提示词资产目录 — 全部 LLM 消费者的段清单、装配单与越界红线（收口层）。

2026-07-27 起，系统内四个 LLM 调用点（Planner / Replyer / meme caption /
记忆压缩）的 system prompt 都从这里装配：`SECTIONS` 是段目录（每段一条元数据：文件、序、
kind、required、适用 scope），`ASSEMBLY` 是每个消费者的有序装配单。改提示词
= 改 `prompts/` 下对应 `.md`（render 时才读盘，改完即生效，无需重启）；给
新消费者配 prompt = 在装配单里挑段。**本文件同时是提示词资产的地图——
维护注记写在这里，不写进 `.md`（那些文件逐字节注入 prompt，放不了给人看
的注释）。**

分层红线（`_FORBIDDEN_KINDS`，build 时结构性校验，契约测试钉死）：
  - persona（voice）不进 Planner——"要不要说话是规则问题，不是性情问题"
    （identity.md 开篇教义）；
  - policy（group_chat_rules）不进 Replyer——它握着 empty_reason 这个小
    否决权，读了参与政策会二次审查已授权的回复，架空 Planner 的决定；
  - protocol / tools 不进 Replyer——一个 prompt 里两套输出 JSON 规范必然
    串台；组稿层也没有工具可调。

失败语义（待办#17 目标 2 前半）：目录内除 tools_usage 外全部 required——
文件缺失/为空时 render 直接 raise（PromptSectionMissing），绝不静默拿残缺
system prompt 继续跑。tools_usage 维持逐工具降级 + warning 的旧语义（单个
工具 .md 缺失只该废那个工具的说明，不该炸整个 prompt）。

voice 的读盘特例：字节仍由 `replyer._load_voice_text()` 负责（本目录的
voice 段惰性委托过去）——`_VOICE_PATH` 是既有契约测试的 monkeypatch 锚点
（test_reply_task_contract::test_missing_voice_file_fails_loudly），且其
ReplyerError 语义要原样进入组稿失败路径。

内容拆分的下一步（方向已定，见 2026-07-27 开发日志）：xml_format 将拆出
Planner/Replyer 共享的世界文档（timeline_syntax / conversation_reading /
envelope_semantics），届时只改本目录与装配单，消费方代码不动。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from qqbot.services.agent_loop.prompt_registry import PromptRegistry

_PROMPTS_DIR = Path(__file__).parent


class PromptAssemblyError(RuntimeError):
    """装配单越界（红线段进了禁入的消费者）或引用了未登记的段。"""


@dataclass(frozen=True)
class SectionSpec:
    """段目录条目。filename=None 表示动态段（source 在 build 时另行注入）。

    scopes=None 表示各 scope 通用；否则 render(scope=...) 落在集合之外时该
    段返回 None（主动跳过，不触发 required——registry 的条件装配通道）。
    """

    name: str
    order: int
    kind: str  # "doc" | "policy" | "protocol" | "tools" | "persona"
    filename: str | None
    required: bool = True
    scopes: tuple[str, ...] | None = None


SECTIONS: dict[str, SectionSpec] = {
    spec.name: spec
    for spec in (
        # ── Planner 五段（order 沿用 llm_planner 的既有约定区间）──
        SectionSpec("identity", 0, "doc", "identity.md"),
        SectionSpec("xml_format", 50, "doc", "xml_format.md"),
        SectionSpec(
            "group_chat_rules",
            100,
            "policy",
            "group_chat_rules.md",
            scopes=("group", "private"),
        ),
        SectionSpec("protocol", 150, "protocol", "protocol.md"),
        SectionSpec("tools_usage", 300, "tools", None, required=False),
        # ── Replyer 两段 ──
        SectionSpec("replyer_composer", 0, "doc", "replyer.md"),
        SectionSpec("voice", 100, "persona", None),
        # ── meme caption 单段 ──
        SectionSpec("meme_caption", 0, "doc", "meme_caption.md"),
        # ── 记忆压缩单段（记忆系统契约 §5）──
        SectionSpec("memory_compaction", 0, "doc", "memory_compaction.md"),
    )
}

ASSEMBLY: dict[str, tuple[str, ...]] = {
    "planner": (
        "identity",
        "xml_format",
        "group_chat_rules",
        "protocol",
        "tools_usage",
    ),
    "replyer": ("replyer_composer", "voice"),
    "caption": ("meme_caption",),
    "memory": ("memory_compaction",),
}

_FORBIDDEN_KINDS: dict[str, frozenset[str]] = {
    "planner": frozenset({"persona"}),
    "replyer": frozenset({"policy", "protocol", "tools"}),
    "caption": frozenset({"persona", "policy", "protocol", "tools"}),
    # 记忆压缩是事实记录员：无人格、无群规、无工具（记忆系统契约 §5.1）。
    "memory": frozenset({"persona", "policy", "protocol", "tools"}),
}


def _validate_assembly(consumer: str, names: tuple[str, ...]) -> None:
    """红线校验：未知消费者/未登记段/kind 越界一律 raise，build 期即炸。"""
    forbidden = _FORBIDDEN_KINDS.get(consumer)
    if forbidden is None:
        raise PromptAssemblyError(f"unknown prompt consumer {consumer!r}")
    for name in names:
        spec = SECTIONS.get(name)
        if spec is None:
            raise PromptAssemblyError(
                f"assembly for {consumer!r} references unknown section {name!r}"
            )
        if spec.kind in forbidden:
            raise PromptAssemblyError(
                f"section {name!r} (kind={spec.kind!r}) is forbidden in the "
                f"{consumer!r} prompt"
            )


def _file_source(spec: SectionSpec) -> Callable[..., str | None]:
    """文件段的懒加载 source：render 时读盘（热更新）、scope 之外返回 None。

    读盘异常（缺失/权限）原样上抛，由 registry 按 required 分流。"""

    def load(scope: str | None = None) -> str | None:
        if (
            spec.scopes is not None
            and scope is not None
            and scope not in spec.scopes
        ):
            return None
        assert spec.filename is not None
        return (_PROMPTS_DIR / spec.filename).read_text(encoding="utf-8")

    return load


def _voice_source() -> str:
    # 惰性 import 避免 catalog ↔ replyer 的模块级环；读盘与 fail-loudly
    # 语义（ReplyerError）保持在 replyer 侧，理由见模块 docstring。
    from qqbot.services.agent_loop.replyer import _load_voice_text

    return _load_voice_text()


def build_registry(
    consumer: str,
    *,
    tool_registry: Any | None = None,
) -> PromptRegistry:
    """按装配单构建某个消费者的 PromptRegistry（含红线校验）。

    tools_usage 段仅在传入 tool_registry 时注册（与 llm_planner 旧行为一致：
    未注入工具注册表的场景——早期骨架/部分测试——不渲染工具用法段）。"""
    names = ASSEMBLY[consumer] if consumer in ASSEMBLY else ()
    _validate_assembly(consumer, names)
    registry = PromptRegistry()
    for name in names:
        spec = SECTIONS[name]
        if name == "tools_usage":
            if tool_registry is None:
                continue
            source: Callable[..., str | None] = tool_registry.usage_docs
        elif name == "voice":
            source = _voice_source
        else:
            source = _file_source(spec)
        registry.register(name, spec.order, source, required=spec.required)
    return registry


def render_system_prompt(
    consumer: str,
    *,
    scope: str | None = None,
    tool_registry: Any | None = None,
) -> str:
    """一步到位：build + render。Replyer / caption 这类单 scope 消费者用。"""
    return build_registry(consumer, tool_registry=tool_registry).render(
        scope=scope
    )

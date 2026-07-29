"""提示词资产目录 — 全部 LLM 消费者的段清单、装配单与越界红线（收口层）。

2026-07-27 起，系统内四个 LLM 调用点（Planner / Replyer / meme caption /
记忆压缩）的 system prompt 都从这里装配：`SECTIONS` 是段目录（每段一条元数据：文件、序、
kind、required、适用 scope），`ASSEMBLY` 是每个消费者的有序装配单。改提示词
= 改 `prompts/` 下对应 `.md`（render 时才读盘，改完即生效，无需重启）；给
新消费者配 prompt = 在装配单里挑段。**本文件同时是提示词资产的地图——
维护注记写在这里，不写进 `.md`（那些文件逐字节注入 prompt，放不了给人看
的注释）。**

分层红线（`_FORBIDDEN_KINDS`，build 时结构性校验，契约测试钉死）：
  - persona（voice）不进 Planner——整张角色卡是"说出来什么样"的权威，把
    12KB 措辞/情绪/形态塞进一个输出 JSON 决策的层，`reasoning` 会开始演戏、
    拿心情当调工具的理由；
  - disposition（参与倾向）不进 Replyer / caption / 图片 / 记忆——它是同一
    个人为"要不要开口"写的窄投影，组稿层已有 voice 这份更全的来源，两份
    并存只会互相打架；
  - policy（group_chat_rules）不进 Replyer——它握着 empty_reason 这个小
    否决权，读了参与政策会二次审查已授权的回复，架空 Planner 的决定；
  - protocol / tools 不进 Replyer——一个 prompt 里两套输出 JSON 规范必然
    串台；组稿层也没有工具可调。

2026-07-29 起 Planner 六段：新增 disposition（`disposition.md`，kind 自成
一类）。此前 identity.md 的开篇教义是"要不要说话是规则问题，不是性情问
题"，group_chat_rules 因此是一份纯相关性闸门；但角色卡把这个人写成对关系
与距离极其敏感，两者并不自洽，落地表现就是只有被点名才开口。现改为：性情
只能**增加**开口的理由（且必须在 timeline 上指得出被拽住的那一处），负面
清单一条都不因性情松动；"说出来什么样"仍然一个字都不进这一层。disposition
与 voice 回答两个不相交的问题（要不要开口 / 说出来什么样），各自是各自那
问的唯一权威，不得互相抄正文。

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
        # ── Planner 六段（order 沿用 llm_planner 的既有约定区间）──
        SectionSpec("identity", 0, "doc", "identity.md"),
        SectionSpec("xml_format", 50, "doc", "xml_format.md"),
        SectionSpec(
            "group_chat_rules",
            100,
            "policy",
            "group_chat_rules.md",
            scopes=("group", "private"),
        ),
        # 紧贴 group_chat_rules 之后：它调制的正是那份闸门，隔开会读成
        # 两套无关的东西。scope 与闸门一致——system loop 没有聊天面，
        # 参与倾向在那里是纯噪音。
        SectionSpec(
            "disposition",
            120,
            "disposition",
            "disposition.md",
            scopes=("group", "private"),
        ),
        SectionSpec("protocol", 150, "protocol", "protocol.md"),
        SectionSpec("tools_usage", 300, "tools", None, required=False),
        # ── Replyer 两段 ──
        SectionSpec("replyer_composer", 0, "doc", "replyer.md"),
        SectionSpec("voice", 100, "persona", None),
        # ── meme caption 单段 ──
        SectionSpec("meme_caption", 0, "doc", "meme_caption.md"),
        # ── timeline 图片客观转录单段（2026-07-28）──
        SectionSpec("image_description", 0, "doc", "image_description.md"),
        # ── look_at_image 带问重看单段（2026-07-28）──
        SectionSpec("image_look", 0, "doc", "image_look.md"),
        # ── 记忆压缩单段（记忆系统契约 §5）──
        SectionSpec("memory_compaction", 0, "doc", "memory_compaction.md"),
    )
}

ASSEMBLY: dict[str, tuple[str, ...]] = {
    "planner": (
        "identity",
        "xml_format",
        "group_chat_rules",
        "disposition",
        "protocol",
        "tools_usage",
    ),
    "replyer": ("replyer_composer", "voice"),
    "caption": ("meme_caption",),
    "image_description": ("image_description",),
    "image_look": ("image_look",),
    "memory": ("memory_compaction",),
}

_FORBIDDEN_KINDS: dict[str, frozenset[str]] = {
    # persona 仍然禁入：2026-07-29 放进来的是 disposition 那一窄段，不是
    # 整张角色卡——红线的目的（措辞/情绪/形态不驱动规划）原样保住。
    "planner": frozenset({"persona"}),
    "replyer": frozenset({"disposition", "policy", "protocol", "tools"}),
    "caption": frozenset(
        {"persona", "disposition", "policy", "protocol", "tools"}
    ),
    # 图片转录是纯记录层：无人格、无群规、无工具。它的输出会被永久写进事件
    # 正文，任何"这图适合怎么用"的判断都会污染下游模型自己的语境合成。
    "image_description": frozenset(
        {"persona", "disposition", "policy", "protocol", "tools"}
    ),
    # 带问重看同样是纯观察层：它只回答被问到的事，人格/群规/协议一律不进。
    "image_look": frozenset(
        {"persona", "disposition", "policy", "protocol", "tools"}
    ),
    # 记忆压缩是事实记录员：无人格、无群规、无工具（记忆系统契约 §5.1）。
    "memory": frozenset(
        {"persona", "disposition", "policy", "protocol", "tools"}
    ),
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

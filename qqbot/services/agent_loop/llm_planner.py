"""LLMPlanner — 调真 LLM 输出 DecisionOutput。

复用 qqbot/core/llm.create_llm() 当 LLM 客户端工厂（纯基础设施层，
不含 v1 业务）。Prompt 与解析逻辑均按 v2 任务与决策契约从零写。

System prompt 不再硬编码 —— 完全交给 PromptRegistry 组装。默认 registry
（build_default_prompt_registry）注册五段：identity / xml_format /
group_chat_rules / protocol / tools usage。人格不再是独立段：决策层（规划、
任务、工具选择、reasoning）无人格，角色卡随 tools/send_message.md 进
tools_usage 段——人格只作用于 send_message 的 content 文本。需要迭代协议、
参与规则或工具说明时直接改对应 .md 即可，不需要碰 planner。

错误兜底：LLM 不可用 / 接口报错 / JSON 不可解析 / schema 不符
一律 fallback 为单一 IdleAction，并把错误细节塞进 reasoning。
不抛异常给 AgentLoop —— AgentLoop 的 planner 异常分支当前只是把这
tick 草草收尾，看不到错误，不利于排障。

契约：任务与决策契约.md §2-§4
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
from pathlib import Path
from typing import Any

from qqbot.core.llm import create_llm
from qqbot.core.logging import get_logger
from qqbot.core.time import CHINA_TIMEZONE
from qqbot.services.agent_loop.decision import (
    Action,
    CallToolAction,
    CompleteTaskAction,
    CreateTaskAction,
    DecisionContext,
    DecisionOutput,
    FailTaskAction,
    IdleAction,
    ImageRef,
    NoteTaskProgressAction,
)
from qqbot.services.agent_loop.image_utils import normalize_image_for_llm
from qqbot.services.agent_loop.projection import (
    _esc_attr,
    _esc_text,
    _safe_json,
)
from qqbot.services.agent_loop.prompt_registry import PromptRegistry
from qqbot.services.agent_loop.tool_registry import ToolRegistry

logger = get_logger(__name__)


# 默认 prompt section order 约定（PromptRegistry 文档 §register）：
#   0   identity           机器身份：决策引擎操作一个 QQ 账号（无人格层）
#   50  xml_format         输入 XML 信封语法与读法
#   100 group_chat_rules   参与规则（什么时候有理由调 send_message）
#   150 protocol           决策协议（任务状态机、JSON 输出规则）
#   300 tools_usage        每个工具的 sibling .md 汇总（含 send_message 工具）
#
# 逻辑递进：你是什么 → 你怎么读输入 → 什么时候需要发言 → 你怎么决定 →
# 你能调什么工具
#
# 人格（小奏角色卡）不设独立段：它只作用于 send_message 的 content 文本，
# 所以直接写在 tools/send_message.md 的 Voice 节里，随 tools_usage 按 scope
# 过滤进 prompt——send_message 限 group/private，system loop 因此天然无人设。
SECTION_IDENTITY = "identity"
SECTION_XML_FORMAT = "xml_format"
SECTION_PROTOCOL = "protocol"
SECTION_GROUP_CHAT_RULES = "group_chat_rules"
SECTION_TOOLS_USAGE = "tools_usage"

def build_default_prompt_registry(
    *,
    tool_registry: ToolRegistry | None = None,
) -> PromptRegistry:
    """v2 默认 system prompt 装配。

    各段从对应 .md 文件懒加载，缺失即静默跳过（PromptRegistry render 会
    忽略空 section）。tools_usage 在 render 时才遍历 ToolRegistry，新增 /
    下架工具立即生效，无需重启 planner。send_message 工具的 sibling .md（含
    小奏角色卡）也走 tools_usage 渲染。
    """
    from qqbot.services.agent_loop import prompts as prompts_pkg
    from qqbot.services.agent_loop.prompts import load_sibling_md

    anchor = prompts_pkg.__file__  # qqbot/services/agent_loop/prompts/__init__.py

    registry = PromptRegistry()

    registry.register(
        SECTION_IDENTITY,
        0,
        lambda: load_sibling_md(anchor, "identity.md"),
    )
    registry.register(
        SECTION_XML_FORMAT,
        50,
        lambda: load_sibling_md(anchor, "xml_format.md"),
    )
    # 参与规则只对有聊天面的 scope 有意义；system loop（审批请求）不渲染，
    # 少一段无关噪音。arity-1 callable：PromptRegistry.render(scope=...) 会把
    # scope 传进来（scope=None 的旧调用不过滤，照常渲染）。
    registry.register(
        SECTION_GROUP_CHAT_RULES,
        100,
        lambda scope=None: ""
        if scope == "system"
        else load_sibling_md(anchor, "group_chat_rules.md"),
    )
    registry.register(
        SECTION_PROTOCOL,
        150,
        lambda: load_sibling_md(anchor, "protocol.md"),
    )

    if tool_registry is not None:
        registry.register(
            SECTION_TOOLS_USAGE,
            300,
            tool_registry.usage_docs,
        )

    return registry


class LLMPlanner:
    """实现 Planner Protocol。线程安全的懒初始化 LLM 客户端。"""

    def __init__(
        self,
        llm_client: Any | None = None,
        tool_registry: ToolRegistry | None = None,
        prompt_registry: PromptRegistry | None = None,
    ) -> None:
        # 测试场景下可注入一个 stub client（提供 ainvoke(messages) 即可）；
        # 生产场景留 None，首次 decide() 时通过 create_llm() 建好缓存。
        self._llm = llm_client
        self._tool_registry = tool_registry
        # prompt_registry 优先：调用方明确传入就用它；否则按 tool_registry
        # 装配默认 registry（identity/xml_format/group_chat_rules/protocol/
        # tools_usage 五段；角色卡在 send_message 的用法文档里）。
        if prompt_registry is None:
            prompt_registry = build_default_prompt_registry(
                tool_registry=tool_registry
            )
        self._prompt_registry = prompt_registry
        self._init_lock = asyncio.Lock()

    async def decide(self, context: DecisionContext) -> DecisionOutput:
        llm = await self._ensure_llm()
        if llm is None:
            return DecisionOutput(
                actions=[IdleAction(reason="llm_unavailable")],
                reasoning="LLM client not configured",
            )

        try:
            messages = _build_messages(
                context, self._tool_registry, self._prompt_registry
            )
            _log_request(context, messages)
        except Exception as exc:
            logger.warning("[llm_planner] build messages failed: {}", exc)
            return DecisionOutput(
                actions=[
                    IdleAction(reason=f"llm_call_error:{type(exc).__name__}")
                ],
                reasoning=str(exc)[:200],
            )

        # ─── JSON 解析重试（任务与决策契约 §7.1：非法输出同 tick 重试至多
        # 2 次，共 3 次调用）───
        # 输出不是合法 JSON 时，把模型原始输出 + 解析错误追加回对话再问一次，
        # 让模型自己修——一次格式抖动不再没收整拍的响应权。传输层异常（网络/
        # 超时）不重试：可能很慢，维持原 llm_call_error 回退等下一次唤醒。
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                raw = await llm.ainvoke(messages)
                text = _extract_text(raw)
                _log_response(context, text)
            except Exception as exc:
                logger.warning("[llm_planner] LLM call failed: {}", exc)
                return DecisionOutput(
                    actions=[
                        IdleAction(
                            reason=f"llm_call_error:{type(exc).__name__}"
                        )
                    ],
                    reasoning=str(exc)[:200],
                )
            try:
                parsed = _parse_json(text)
            except Exception as exc:
                logger.warning(
                    "[llm_planner] JSON parse failed (attempt {}/{}): {} raw={!r}",
                    attempt,
                    max_attempts,
                    exc,
                    text[:200],
                )
                if attempt >= max_attempts:
                    return DecisionOutput(
                        actions=[
                            IdleAction(
                                reason=f"llm_json_error:{type(exc).__name__}"
                            )
                        ],
                        reasoning=(
                            f"unparseable after {max_attempts} attempts: "
                            f"{text[:120]}"
                        ),
                    )
                from langchain_core.messages import AIMessage, HumanMessage

                messages = list(messages) + [
                    AIMessage(content=text),
                    HumanMessage(
                        content=(
                            "Your previous response could not be parsed as "
                            f"the required JSON ({type(exc).__name__}: {exc}). "
                            "Re-emit your COMPLETE decision as ONE valid JSON "
                            "object only — no prose, no markdown fences."
                        )
                    ),
                ]
                continue
            return _parse_decision_output(parsed)
        # for 循环内必 return；此处只为类型完备
        return DecisionOutput(
            actions=[IdleAction(reason="llm_json_error:exhausted")],
        )

    async def _ensure_llm(self) -> Any:
        if self._llm is not None:
            return self._llm
        async with self._init_lock:
            if self._llm is None:
                self._llm = await create_llm()
            return self._llm


def _build_messages(
    context: DecisionContext,
    tool_registry: ToolRegistry | None,
    prompt_registry: PromptRegistry,
) -> list[Any]:
    """构造 chat 输入。langchain_core.messages 在 langchain_openai 已是必依赖。

    System prompt 完全由 PromptRegistry.render() 输出 —— 默认装配里包含
    identity / xml_format / group_chat_rules / protocol / tools_usage 五段，
    每段在自己的 .md 文件里独立维护。

    HumanMessage 的 text block 用 XML 信封而非 JSON 拼装：timeline 里每条
    item 的 render 字段本身就是 `<message ...>` / `<tool-call ...>`（发言也是
    send_message 工具的 tool-call）/ `<notice ...>` 等独立标签，所以外层再用 `<agent-input>` /
    `<timeline>` 标签嵌套时上下引用关系（reply、at、tool_call ↔ result）
    依然连贯，而不会被 JSON 字符串转义压平成扁平的字段表。

    多模态：本系统只对接 VLM，timeline 渲染里出现 `<image hash="..."/>` 的
    每张已落盘图片都会随 HumanMessage 一并发出。content 结构 = 一个 text
    block（XML payload）+ 若干 image_url block（base64 data URL），按
    hash 全局去重，LLM 靠文本里的 hash 字符串与图片对齐。
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    # 按当前 loop 的 scope 过滤 catalog **与 tools_usage 文档**：allowed_scopes 限定
    # 的工具（如 ban / respond_to_group_join_request 仅 group）既不出现在别的 scope 的 tool-catalog
    # 里，其用法文档也不进别的 scope 的 system prompt——否则群专用工具的说明会泄漏
    # 到 system loop、system 专用工具的说明会泄漏到群 loop，徒增提示噪音与误判
    # （§2.2；catalog 与 usage 同一把 scope 尺子）。
    scope = context.scope_key.split(":", 1)[0]
    system_content = prompt_registry.render(scope=scope)
    tool_catalog = (
        tool_registry.catalog(scope) if tool_registry is not None else []
    )
    xml_text = _render_input_xml(context, tool_catalog)

    text_block = {"type": "text", "text": xml_text}
    image_blocks = _build_image_blocks(context)
    human_content: list[dict] = [text_block, *image_blocks]

    return [
        SystemMessage(content=system_content),
        HumanMessage(content=human_content),
    ]


def _render_input_xml(
    context: DecisionContext, tool_catalog: list[dict]
) -> str:
    """拼装喂给 LLM 的 XML 信封。

    结构（顺序按**变化频率升序**排列——前缀缓存契约，2026-07-12）：

      <agent-input scope="..." bot_qq="..." bot_role="...">
        <tool-catalog>
          <tool name="..." description="...">
            <arguments-schema>{json schema 文本}</arguments-schema>
          </tool>
        </tool-catalog>
        <saved-memes>
          <meme hash="..." saved_at="...">描述</meme>
        </saved-memes>                                  (有收藏才出)
        <timeline>
          {item.render \n item.render ...}
        </timeline>
        <active-tasks>
          <task task_id="..." state="..." description="...">
            <related-tools>tool1,tool2</related-tools>
            <triggered-by event_id="..."/>            (可选)
            <pending-tool-call-ids>tc1,tc2</pending-tool-call-ids>  (有才出)
            <progress-notes>
              <note time="...">...</note>
            </progress-notes>
          </task>
        </active-tasks>
        <current now="..." tick="N"/>
        <validation-error>attempt N rejected: ...</validation-error>       (仅校验重试)
      </agent-input>

    缓存契约（2026-07-12）：OpenAI 系 API 的自动前缀缓存要求前缀**逐字节
    一致**。now=/tick= 每拍必变，曾是 <agent-input> 的头属性，把可缓存前缀
    掐断在 system prompt 末尾——tool-catalog（部署内静态）和 timeline（追加
    为主，窗口起点锚定见 projection）每拍全价重计费。现按变化频率排序：
    头属性只留 scope/bot_qq/bot_role（稳定/极少变）；active-tasks 任务活跃期
    逐拍变（pending_tool_call_ids 随工具收口增删），排到 timeline 之后；
    每拍必变的 now/tick 沉为尾部 <current/>；validation-error 只在同 tick
    校验重试出现（契约 §7.1），放最尾——同拍重试可复用直到 <current/> 的
    前缀，且作为最后一行对模型最显著。timeline 仍然紧邻输出位置（其后只有
    寥寥数行），recency bias 不受影响。

    工具结果只在 <timeline> 的 <tool-call status="complete"> 行呈现一次
    （2026-07-02 删除了 <pending-tool-results> 区——同一调用双重渲染、且无
    消费切割地每拍重复出现，是模型复读的直接诱饵）。模型的跨拍自我记忆
    2026-07-06 起也在 timeline 内——最近 K 条 decision_emitted 渲染为
    <my-thought> 行（投影层负责，见 projection.build_timeline；独立的
    <last-reasoning> 区块已删除）。
    """
    parts: list[str] = []
    # bot_qq 可选（值来自 context.bot_user_id）：未注入（启动初期 bot_registry
    # 还空、或测试场景）时不渲染属性；此时模型仍可靠别人
    # <reply ... from_self="true"/> 的服务端标注识别"这条是回复我的"
    # （from_self 由投影层解析，不依赖本属性）。属性名用 _qq 后缀与
    # sender_qq / from_qq 同一 ID 空间记法。
    bot_attr = (
        f' bot_qq="{_esc_attr(context.bot_user_id)}"'
        if context.bot_user_id
        else ""
    )
    # bot_role 同样可选：sweep 未完成时 None，不渲染。这是**折叠快照**，仅作给
    # LLM 的角色提示——真正判 bot 权限时工具会**实时**复查当前角色（见
    # tool_registry._effective_bot_role），快照过期也不会误判。故 prompt 明确要求
    # LLM 不要据此快照"该调不调"：真无权限时工具返回 permission_denied_bot_role，
    # 快照过低但已实际升权的调用照样能过（消除规划层假阴性）。
    role_attr = (
        f' bot_role="{_esc_attr(context.bot_role)}"' if context.bot_role else ""
    )
    # 头属性只留稳定字段（scope 恒定 / bot_qq 启动后恒定 / bot_role 极少变）
    # ——每拍必变的 now/tick 在尾部 <current/>，见本函数 docstring 的缓存契约。
    parts.append(
        f'<agent-input scope="{_esc_attr(context.scope_key)}"'
        f"{bot_attr}{role_attr}>"
    )

    parts.append("<tool-catalog>")
    for tool in tool_catalog:
        name = _esc_attr(str(tool.get("name", "")))
        desc = _esc_attr(str(tool.get("description", "")))
        schema_json = _safe_json(tool.get("arguments_schema") or {})
        # required_permission / required_bot_role 作为属性透出，让 LLM 调用前
        # 即可判断 "我能不能调" 而不必非要触发 tool_failed 再学习。tool_registry
        # 的 catalog() 已经兜底过缺失值；required_bot_role=None 的工具不渲染该
        # 属性（绝大多数工具不需要 bot 是管理员，省 token + 减噪音）。
        req_perm = _esc_attr(str(tool.get("required_permission", "GUEST")))
        req_bot_role = tool.get("required_bot_role")
        role_attr = (
            f' required_bot_role="{_esc_attr(str(req_bot_role))}"'
            if req_bot_role
            else ""
        )
        parts.append(
            f'<tool name="{name}" description="{desc}" '
            f'required_permission="{req_perm}"{role_attr}>'
            f"<arguments-schema>{_esc_text(schema_json)}</arguments-schema>"
            f"</tool>"
        )
    parts.append("</tool-catalog>")

    # ─── 表情包收藏夹（有才渲染）：meme 工具凭 hash 精确操作收藏的选图目录。
    # 空收藏整段省略——不给模型一个空 <saved-memes> 去好奇。───
    saved_memes = getattr(context, "saved_memes", None) or []
    if saved_memes:
        parts.append("<saved-memes>")
        for meme in saved_memes:
            saved_attr = _esc_attr(
                meme.saved_at.isoformat(timespec="seconds")
            )
            parts.append(
                f'<meme hash="{_esc_attr(meme.file_hash)}" '
                f'saved_at="{saved_attr}">'
                f"{_esc_text(meme.description)}</meme>"
            )
        parts.append("</saved-memes>")

    parts.append("<timeline>")
    # 每条 item.render 单独占一行，纯字符串拼接已足够；不再 JSON 包装。
    for item in context.timeline:
        parts.append(item.render)
    parts.append("</timeline>")

    # active-tasks 在 timeline 之后：任务活跃期它逐拍变（pending_tool_call_ids
    # 随工具收口增删），放前面会在多工具工作流里逐拍掐断 timeline 的缓存前缀；
    # 放这里还让"当前承诺"紧邻决策位置，显著性只增不减。
    parts.append("<active-tasks>")
    for task in context.active_tasks:
        parts.append(_render_task_xml(task))
    parts.append("</active-tasks>")

    # ─── 每拍必变的时钟字段，沉底（缓存契约见本函数 docstring）───
    # 时区契约：所有暴露给 LLM 的时间都是北京时间（与数据库写入侧 china_now()
    # 一致）。caller 传错时区时 astimezone() 兜底，naive datetime 假设它就是
    # 北京时间。
    now = context.now
    if now.tzinfo is None:
        now = now.replace(tzinfo=CHINA_TIMEZONE)
    else:
        now = now.astimezone(CHINA_TIMEZONE)
    parts.append(
        f'<current now="{_esc_attr(now.isoformat())}" '
        f'tick="{context.tick_seq}"/>'
    )

    # ─── 同 tick 校验重试反馈（仅重试调用渲染，契约 §7.1）───
    validation_feedback = getattr(context, "validation_feedback", None)
    if validation_feedback:
        parts.append(
            "<validation-error>"
            f"{_esc_text(validation_feedback)}</validation-error>"
        )

    parts.append("</agent-input>")
    return "\n".join(parts)


def _render_task_xml(task: Any) -> str:
    """单条 task → <task ...> 块。
    related_tools / pending_tool_call_ids 用逗号串方便 LLM 扫读；progress_notes
    每条单独 <note>，时间属性走 _esc_attr。"""
    inner: list[str] = []
    related = ",".join(task.related_tools or [])
    if related:
        inner.append(f"<related-tools>{_esc_text(related)}</related-tools>")
    trig = getattr(task, "triggered_by_event_id", None)
    if trig:
        inner.append(f'<triggered-by event_id="{_esc_attr(str(trig))}"/>')
    pending = ",".join(task.pending_tool_call_ids or [])
    if pending:
        inner.append(
            f"<pending-tool-call-ids>{_esc_text(pending)}"
            f"</pending-tool-call-ids>"
        )
    notes = getattr(task, "progress_notes", None) or []
    if notes:
        note_parts = ["<progress-notes>"]
        for n in notes:
            note_parts.append(
                f'<note time="{_esc_attr(n.at.isoformat())}">'
                f"{_esc_text(n.note)}</note>"
            )
        note_parts.append("</progress-notes>")
        inner.append("".join(note_parts))
    # task_id= 而非裸 id=：与动作 JSON 里要回填的字段名（complete_task /
    # call_tool 的 "task_id"）同名直抄，且与 message_id / event_id 空间区分。
    return (
        f'<task task_id="{_esc_attr(task.task_id)}" '
        f'state="{_esc_attr(task.state)}" '
        f'description="{_esc_attr(task.description)}">'
        f"{''.join(inner)}</task>"
    )


def _build_image_blocks(context: DecisionContext) -> list[dict]:
    """汇总 timeline 中所有 ImageRef，按 hash 去重后读盘 + base64，
    返回 OpenAI 兼容的 content blocks。

    每张图前面**挂一个文本 label `↓ image hash=<sha256>`**，让 LLM 能把
    XML timeline 里出现的 `<image hash="XXX"/>` 占位符和实际图像 bytes
    一一对应起来 —— 否则一长串 image_url 紧跟在 XML 文本后面，LLM 只能
    靠"按出现顺序数"来对位，3 张图以上就经常错位（用户说"上上一张图"
    时定位不到对应像素）。label 用 ↓ 箭头明示"指向下一块"，hash 是
    universal binder。

    GIF 在编码前取首帧转 PNG，兼容只接受 JPG/PNG/WebP/ICO 的 VLM 网关。
    读盘或转换失败的图跳过 label + 图 —— text 里
    的 `<image hash="..."/>` 占位还在，LLM 知道存在但看不到，避免整
    tick 失败。
    """
    seen: set[str] = set()
    ordered: list[ImageRef] = []
    for item in context.timeline:
        for ref in getattr(item, "images", ()) or ():
            if ref.file_hash in seen:
                continue
            seen.add(ref.file_hash)
            ordered.append(ref)

    blocks: list[dict] = []
    for ref in ordered:
        try:
            data = Path(ref.local_path).read_bytes()
        except OSError as exc:
            logger.warning(
                "[llm_planner] image read failed: {} hash={} path={}",
                exc,
                ref.file_hash,
                ref.local_path,
            )
            continue
        try:
            data, mime = normalize_image_for_llm(data, ref.mime or "image/png")
        except Exception as exc:
            logger.warning(
                "[llm_planner] image conversion failed: {} hash={} path={}",
                exc,
                ref.file_hash,
                ref.local_path,
            )
            continue
        b64 = base64.b64encode(data).decode("ascii")
        blocks.append(
            {"type": "text", "text": f"↓ image hash={ref.file_hash}"}
        )
        blocks.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
            }
        )
    return blocks


def _log_request(context: DecisionContext, messages: list[Any]) -> None:
    """喂给 LLM 的完整内容打到 INFO 日志，方便核对事件流是否合理。

    布局优先可读：用换行 + 分隔条把 system / user / response 三段切开，
    避免一行 5KB+ 撑爆 terminal。system prompt 只打首尾几行 + 长度，
    因为它基本静态（identity + protocol + group_chat_rules + tools usage）；
    user 段（XML 信封）是 tick 之间真正变化的东西，原样全打。
    """
    # messages = [SystemMessage, HumanMessage]
    sys_msg, human_msg = messages[0], messages[1]
    system_text = getattr(sys_msg, "content", "") or ""
    human_content = getattr(human_msg, "content", "") or ""

    if isinstance(human_content, list):
        text_blocks = [
            b.get("text", "")
            for b in human_content
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        image_count = sum(
            1
            for b in human_content
            if isinstance(b, dict) and b.get("type") == "image_url"
        )
        user_text = "\n".join(text_blocks)
    else:
        user_text = str(human_content)
        image_count = 0

    sep = "=" * 80
    logger.info(
        "\n{sep}\n[llm_planner] → LLM  scope={scope} tick={tick} "
        "system_prompt_chars={syslen} user_xml_chars={ulen} "
        "image_blocks={imgs}\n"
        "{sep}\n"
        "── system_prompt (head 400 / tail 200) ──\n{sys_head}\n…\n{sys_tail}\n"
        "── user_xml ──\n{user}\n{sep}",
        sep=sep,
        scope=context.scope_key,
        tick=context.tick_seq,
        syslen=len(system_text),
        ulen=len(user_text),
        imgs=image_count,
        sys_head=system_text[:400],
        sys_tail=system_text[-200:] if len(system_text) > 600 else "",
        user=user_text,
    )


def _log_response(context: DecisionContext, text: str) -> None:
    sep = "=" * 80
    logger.info(
        "\n{sep}\n[llm_planner] ← LLM  scope={scope} tick={tick} "
        "response_chars={rlen}\n{sep}\n{body}\n{sep}",
        sep=sep,
        scope=context.scope_key,
        tick=context.tick_seq,
        rlen=len(text),
        body=text,
    )


def _extract_text(message: Any) -> str:
    """langchain BaseMessage.content 在多模态/分片场景下可能是 list[dict]，
    统一拍平成 str。"""
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for chunk in content:
            if isinstance(chunk, dict) and "text" in chunk:
                parts.append(str(chunk["text"]))
            elif isinstance(chunk, str):
                parts.append(chunk)
        return "".join(parts)
    return str(content)


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


def _parse_json(text: str) -> Any:
    """容忍 markdown 围栏。LLM 偶尔会无视 "no fences" 指令。"""
    m = _FENCE_RE.match(text)
    body = m.group(1) if m else text
    return json.loads(body)


def _parse_decision_output(parsed: Any) -> DecisionOutput:
    if not isinstance(parsed, dict):
        return DecisionOutput(
            actions=[IdleAction(reason="llm_schema_error:not_object")],
            reasoning=str(parsed)[:200],
        )

    reasoning = parsed.get("reasoning")
    reasoning_str = reasoning if isinstance(reasoning, str) else None
    raw_actions = parsed.get("actions") or []
    if not isinstance(raw_actions, list):
        return DecisionOutput(
            actions=[IdleAction(reason="llm_schema_error:actions_not_list")],
            reasoning=reasoning_str,
        )

    actions: list[Action] = []
    for raw in raw_actions:
        action = _parse_action(raw)
        if action is None:
            return DecisionOutput(
                actions=[IdleAction(reason="llm_schema_error:bad_action")],
                reasoning=f"bad action: {raw}"[:200],
            )
        actions.append(action)

    if not actions:
        actions = [IdleAction(reason="empty_actions")]

    return DecisionOutput(actions=actions, reasoning=reasoning_str)


def _parse_action(raw: Any) -> Action | None:
    if not isinstance(raw, dict):
        return None
    t = raw.get("type")
    try:
        if t == "idle":
            return IdleAction(reason=str(raw.get("reason", "")))
        if t == "create_task":
            return CreateTaskAction(
                description=str(raw.get("description", "")),
                related_tools=_as_str_list(raw.get("related_tools")),
                parent_task_id=raw.get("parent_task_id") or None,
                task_ref=raw.get("task_ref") or None,
                triggered_by_event_id=raw.get("triggered_by_event_id") or None,
            )
        if t == "call_tool":
            args = raw.get("arguments") or {}
            return CallToolAction(
                tool_name=str(raw.get("tool_name", "")),
                arguments=args if isinstance(args, dict) else {},
                task_id=raw.get("task_id") or None,
                task_ref=raw.get("task_ref") or None,
                triggered_by_event_id=raw.get("triggered_by_event_id") or None,
            )
        # NOTE: t == "reply" 已弃用——发言现在是工具，走
        # {"type":"call_tool","tool_name":"send_message","arguments":{...}}。
        if t == "complete_task":
            return CompleteTaskAction(
                task_id=str(raw.get("task_id", "")),
                result_summary=raw.get("result_summary") or None,
            )
        if t == "fail_task":
            return FailTaskAction(
                task_id=str(raw.get("task_id", "")),
                reason=str(raw.get("reason", "")),
            )
        if t == "note_task_progress":
            note = raw.get("note")
            return NoteTaskProgressAction(
                task_id=str(raw.get("task_id", "")),
                note=str(note) if note is not None else "",
            )
    except Exception:
        return None
    return None


def _as_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(x) for x in value]

"""LLMPlanner — 调真 LLM 输出 DecisionOutput。

复用 qqbot/core/llm.create_llm() 当 LLM 客户端工厂（纯基础设施层，
不含 v1 业务）。Prompt 与解析逻辑均按 v2 任务与决策契约从零写。

System prompt 不再硬编码 —— 完全交给 PromptRegistry 组装。默认 registry
（build_default_prompt_registry）注册四段：persona / protocol / reply
usage / tools usage。需要把人设、协议、动作文档或工具说明独立迭代时
直接改对应 .md 即可，不需要碰 planner。

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
from qqbot.services.agent_loop.projection import _esc_attr, _esc_text, _safe_json
from qqbot.services.agent_loop.prompt_registry import PromptRegistry
from qqbot.services.agent_loop.tool_registry import ToolRegistry

logger = get_logger(__name__)


# 默认 prompt section order 约定（PromptRegistry 文档 §register）：
#   0   persona            人设
#   50  xml_format         输入 XML 信封语法与读法
#   100 protocol           决策协议（任务状态机、JSON 输出规则）
#   150 group_chat_rules   群聊行为规范（什么时候说话 / 怎么称呼对方）
#   300 tools_usage        每个工具的 sibling .md 汇总（含 reply 工具）
#
# 逻辑递进：你是谁 → 你怎么读输入 → 你怎么决定 → 你怎么社交 → 你能调什么工具
#
# 旧版的 SECTION_REPLY_USAGE 段（prompts/reply.md）已经移除：reply 现在是
# 普通工具，其用法文档 tools/reply.md 自动通过 ToolRegistry.usage_docs()
# 进入 tools_usage 段，与 websearch / search_history 同构。
SECTION_PERSONA = "persona"
SECTION_XML_FORMAT = "xml_format"
SECTION_PROTOCOL = "protocol"
SECTION_GROUP_CHAT_RULES = "group_chat_rules"
SECTION_TOOLS_USAGE = "tools_usage"

def build_default_prompt_registry(
    *,
    persona_text: str | None = None,
    tool_registry: ToolRegistry | None = None,
) -> PromptRegistry:
    """v2 默认 system prompt 装配。

    各段从对应 .md 文件懒加载，缺失即静默跳过（PromptRegistry render 会
    忽略空 section）。tools_usage 在 render 时才遍历 ToolRegistry，新增 /
    下架工具立即生效，无需重启 planner。reply 工具的 sibling .md 也走
    tools_usage 渲染。
    """
    from qqbot.services.agent_loop import prompts as prompts_pkg
    from qqbot.services.agent_loop.prompts import load_sibling_md

    anchor = prompts_pkg.__file__  # qqbot/services/agent_loop/prompts/__init__.py

    registry = PromptRegistry()

    if persona_text and persona_text.strip():
        registry.register(SECTION_PERSONA, 0, persona_text.strip())

    registry.register(
        SECTION_XML_FORMAT,
        50,
        lambda: load_sibling_md(anchor, "xml_format.md"),
    )
    registry.register(
        SECTION_PROTOCOL,
        100,
        lambda: load_sibling_md(anchor, "protocol.md"),
    )
    registry.register(
        SECTION_GROUP_CHAT_RULES,
        150,
        lambda: load_sibling_md(anchor, "group_chat_rules.md"),
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
        persona_text: str | None = None,
        prompt_registry: PromptRegistry | None = None,
    ) -> None:
        # 测试场景下可注入一个 stub client（提供 ainvoke(messages) 即可）；
        # 生产场景留 None，首次 decide() 时通过 create_llm() 建好缓存。
        self._llm = llm_client
        self._tool_registry = tool_registry
        # 人设文本（由 plugin 启动期从 services/agent_loop/prompts/persona.md
        # 读入）—— 只在 prompt_registry 未注入时用于装配默认 registry。
        # 装好后通过 registry render 输出，不再单独存。
        # prompt_registry 优先：调用方明确传入就用它；否则按 persona +
        # tool_registry 装配默认 registry。
        if prompt_registry is None:
            prompt_registry = build_default_prompt_registry(
                persona_text=persona_text, tool_registry=tool_registry
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
            raw = await llm.ainvoke(messages)
            text = _extract_text(raw)
            _log_response(context, text)
        except Exception as exc:
            logger.warning("[llm_planner] LLM call failed: {}", exc)
            return DecisionOutput(
                actions=[
                    IdleAction(reason=f"llm_call_error:{type(exc).__name__}")
                ],
                reasoning=str(exc)[:200],
            )

        try:
            parsed = _parse_json(text)
        except Exception as exc:
            logger.warning(
                "[llm_planner] JSON parse failed: {} raw={!r}",
                exc,
                text[:200],
            )
            return DecisionOutput(
                actions=[
                    IdleAction(reason=f"llm_json_error:{type(exc).__name__}")
                ],
                reasoning=f"unparseable: {text[:120]}",
            )

        return _parse_decision_output(parsed)

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
    persona / protocol / reply_usage / tools_usage 四段，每段在自己的
    .md 文件里独立维护。

    HumanMessage 的 text block 用 XML 信封而非 JSON 拼装：timeline 里每条
    item 的 render 字段本身就是 `<message ...>` / `<agent-reply ...>` /
    `<tool-call ...>` 等独立标签，所以外层再用 `<agent-input>` /
    `<timeline>` 标签嵌套时上下引用关系（reply、at、tool_call ↔ result）
    依然连贯，而不会被 JSON 字符串转义压平成扁平的字段表。

    多模态：本系统只对接 VLM，timeline 渲染里出现 `<image hash="..."/>` 的
    每张已落盘图片都会随 HumanMessage 一并发出。content 结构 = 一个 text
    block（XML payload）+ 若干 image_url block（base64 data URL），按
    hash 全局去重，LLM 靠文本里的 hash 字符串与图片对齐。
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    system_content = prompt_registry.render()

    tool_catalog = tool_registry.catalog() if tool_registry is not None else []
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

    结构（顺序刻意从静态参考 → 动态状态 → 时间线尾部，让 timeline 紧贴
    LLM 输出位置，吃满 recency bias）：

      <agent-input scope="..." now="..." tick="N">
        <tool-catalog>
          <tool name="..." description="...">
            <arguments-schema>{json schema 文本}</arguments-schema>
          </tool>
        </tool-catalog>
        <active-tasks>
          <task id="..." state="..." description="...">
            <related-tools>tool1,tool2</related-tools>
            <triggered-by event_id="..."/>            (可选)
            <pending-tool-call-ids>tc1,tc2</pending-tool-call-ids>  (有才出)
            <progress-notes>
              <note at="...">...</note>
            </progress-notes>
          </task>
        </active-tasks>
        <pending-tool-results>
          <tool-result id="..." name="..." status="...">
            <args>{json}</args>
            <result>{json}</result>            或
            <error kind="...">message</error>
          </tool-result>
        </pending-tool-results>
        <timeline>
          {item.render \n item.render ...}
        </timeline>
      </agent-input>
    """
    parts: list[str] = []
    # bot_user_id 可选：未注入（启动初期 bot_registry 还空、或测试场景）
    # 时不渲染属性，让模型走旧路径（看 <agent-reply> 引用反推自己是谁）。
    bot_attr = (
        f' bot_user_id="{_esc_attr(context.bot_user_id)}"'
        if context.bot_user_id
        else ""
    )
    # 时区契约：所有暴露给 LLM 的时间都是北京时间（与数据库写入侧 china_now()
    # 一致）。caller 传错时区时 astimezone() 兜底，naive datetime 走 fromtz
    # 假设它就是北京时间。
    now = context.now
    if now.tzinfo is None:
        now = now.replace(tzinfo=CHINA_TIMEZONE)
    else:
        now = now.astimezone(CHINA_TIMEZONE)
    parts.append(
        f'<agent-input scope="{_esc_attr(context.scope_key)}" '
        f'now="{_esc_attr(now.isoformat())}" '
        f'tick="{context.tick_seq}"{bot_attr}>'
    )

    parts.append("<tool-catalog>")
    for tool in tool_catalog:
        name = _esc_attr(str(tool.get("name", "")))
        desc = _esc_attr(str(tool.get("description", "")))
        schema_json = _safe_json(tool.get("arguments_schema") or {})
        parts.append(
            f'<tool name="{name}" description="{desc}">'
            f"<arguments-schema>{_esc_text(schema_json)}</arguments-schema>"
            f"</tool>"
        )
    parts.append("</tool-catalog>")

    parts.append("<active-tasks>")
    for task in context.active_tasks:
        parts.append(_render_task_xml(task))
    parts.append("</active-tasks>")

    parts.append("<pending-tool-results>")
    for tr in context.pending_tool_results:
        parts.append(_render_tool_result_xml(tr))
    parts.append("</pending-tool-results>")

    parts.append("<timeline>")
    # 每条 item.render 单独占一行，纯字符串拼接已足够；不再 JSON 包装。
    for item in context.timeline:
        parts.append(item.render)
    parts.append("</timeline>")

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
                f'<note at="{_esc_attr(n.at.isoformat())}">'
                f"{_esc_text(n.note)}</note>"
            )
        note_parts.append("</progress-notes>")
        inner.append("".join(note_parts))
    return (
        f'<task id="{_esc_attr(task.task_id)}" '
        f'state="{_esc_attr(task.state)}" '
        f'description="{_esc_attr(task.description)}">'
        f"{''.join(inner)}</task>"
    )


def _render_tool_result_xml(r: Any) -> str:
    """单条 ToolResultView → <tool-result> 块。
    args / result 仍以 JSON 字符串塞进文本节点（结构化对象，无注入风险，
    且 LLM 对 JSON 内容的理解能力很强），整段被 XML 包裹。"""
    parts: list[str] = [
        f'<tool-result id="{_esc_attr(r.tool_call_id)}" '
        f'name="{_esc_attr(r.tool_name)}" status="{_esc_attr(r.status)}">'
    ]
    parts.append(f"<args>{_esc_text(_safe_json(r.arguments or {}))}</args>")
    if r.status == "succeeded":
        parts.append(f"<result>{_esc_text(_safe_json(r.result))}</result>")
    elif r.status == "failed":
        parts.append(
            f'<error kind="{_esc_attr(str(r.error_kind or ""))}">'
            f"{_esc_text(str(r.error_message or ''))}</error>"
        )
    parts.append("</tool-result>")
    return "".join(parts)


def _build_image_blocks(context: DecisionContext) -> list[dict]:
    """汇总 timeline 中所有 ImageRef，按 hash 去重后读盘 + base64，
    返回 OpenAI 兼容的 content blocks。

    每张图前面**挂一个文本 label `↓ image hash=<sha256>`**，让 LLM 能把
    XML timeline 里出现的 `<image hash="XXX"/>` 占位符和实际图像 bytes
    一一对应起来 —— 否则一长串 image_url 紧跟在 XML 文本后面，LLM 只能
    靠"按出现顺序数"来对位，3 张图以上就经常错位（用户说"上上一张图"
    时定位不到对应像素）。label 用 ↓ 箭头明示"指向下一块"，hash 是
    universal binder。

    读盘失败的图（文件已被清理 / 权限异常）跳过 label + 图 —— text 里
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
        b64 = base64.b64encode(data).decode("ascii")
        mime = ref.mime or "image/png"
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
    因为它基本静态（persona + protocol + reply.md + tools usage）；
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
            )
        # NOTE: t == "reply" 已弃用——reply 现在是工具，走
        # {"type":"call_tool","tool_name":"reply","arguments":{...}}。
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

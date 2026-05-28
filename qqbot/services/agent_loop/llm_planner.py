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
    ReplyAction,
)
from qqbot.services.agent_loop.prompt_registry import PromptRegistry
from qqbot.services.agent_loop.tool_registry import ToolRegistry

logger = get_logger(__name__)


# 默认 prompt section order 约定（PromptRegistry 文档 §register）：
#   0   persona       人设
#   100 protocol      决策协议（任务状态机、JSON 输出规则）
#   200 reply_usage   reply.content 段的 OneBot V11 用法
#   300 tools_usage   每个工具的 sibling .md 汇总
SECTION_PERSONA = "persona"
SECTION_PROTOCOL = "protocol"
SECTION_REPLY_USAGE = "reply_usage"
SECTION_TOOLS_USAGE = "tools_usage"

def build_default_prompt_registry(
    *,
    persona_text: str | None = None,
    tool_registry: ToolRegistry | None = None,
) -> PromptRegistry:
    """v2 默认 system prompt 装配。

    各段从对应 .md 文件懒加载，缺失即静默跳过（PromptRegistry render 会
    忽略空 section）。tools_usage 在 render 时才遍历 ToolRegistry，新增 /
    下架工具立即生效，无需重启 planner。
    """
    from qqbot.services.agent_loop import prompts as prompts_pkg
    from qqbot.services.agent_loop.prompts import load_sibling_md

    anchor = prompts_pkg.__file__  # qqbot/services/agent_loop/prompts/__init__.py

    registry = PromptRegistry()

    if persona_text and persona_text.strip():
        registry.register(SECTION_PERSONA, 0, persona_text.strip())

    registry.register(
        SECTION_PROTOCOL,
        100,
        lambda: load_sibling_md(anchor, "protocol.md"),
    )
    registry.register(
        SECTION_REPLY_USAGE,
        200,
        lambda: load_sibling_md(anchor, "reply.md"),
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
        # 人设文本（由 plugin 启动期从 qqbot/persona.md 读入）—— 只在
        # prompt_registry 未注入时用于装配默认 registry。装好后通过
        # registry render 输出，不再单独存。
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
            raw = await llm.ainvoke(messages)
            text = _extract_text(raw)
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

    多模态：本系统只对接 VLM，timeline 渲染里出现 `<image hash="..."/>` 的
    每张已落盘图片都会随 HumanMessage 一并发出。content 结构 = 一个 text
    block（JSON payload）+ 若干 image_url block（base64 data URL），按
    hash 全局去重，LLM 靠文本里的 hash 字符串与图片对齐。
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    system_content = prompt_registry.render()

    tool_catalog = tool_registry.catalog() if tool_registry is not None else []
    user_payload = {
        "scope_key": context.scope_key,
        "now": context.now.isoformat(),
        "tick_seq": context.tick_seq,
        "tool_catalog": tool_catalog,
        "timeline": [_render_timeline(t) for t in context.timeline],
        "active_tasks": [_render_task(t) for t in context.active_tasks],
        "pending_tool_results": [
            _render_tool_result(r) for r in context.pending_tool_results
        ],
    }

    text_block = {
        "type": "text",
        "text": json.dumps(user_payload, ensure_ascii=False),
    }
    image_blocks = _build_image_blocks(context)
    human_content: list[dict] = [text_block, *image_blocks]

    return [
        SystemMessage(content=system_content),
        HumanMessage(content=human_content),
    ]


def _build_image_blocks(context: DecisionContext) -> list[dict]:
    """汇总 timeline 中所有 ImageRef，按 hash 去重后读盘 + base64，
    返回 OpenAI 兼容的 image_url content blocks。

    读盘失败的图（文件已被清理 / 权限异常）跳过 —— text 里的 `<image
    hash="..."/>` 占位还在，LLM 知道存在但看不到，避免整 tick 失败。
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
            {
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
            }
        )
    return blocks


def _render_timeline(item: Any) -> dict:
    return {
        "kind": item.kind,
        "occurred_at": item.occurred_at.isoformat(),
        "render": item.render,
    }


def _render_task(task: Any) -> dict:
    return {
        "task_id": task.task_id,
        "description": task.description,
        "state": task.state,
        "related_tools": task.related_tools,
        "pending_tool_call_ids": task.pending_tool_call_ids,
        # 锚点：search_history 等工具据此查 task 起点之前的事件。可能为 None
        # （旧任务 / LLM 当时没填）— 工具侧自行兜底。
        "triggered_by_event_id": getattr(task, "triggered_by_event_id", None),
        # 跨 tick 思考：上一轮 LLM 自己留的进度笔记，最多 N 条由 Projector 截断。
        "progress_notes": [
            {"at": n.at.isoformat(), "note": n.note}
            for n in getattr(task, "progress_notes", [])
        ],
    }


def _render_tool_result(r: Any) -> dict:
    return {
        "tool_call_id": r.tool_call_id,
        "tool_name": r.tool_name,
        "status": r.status,
        "result": r.result,
        "error_kind": r.error_kind,
        "error_message": r.error_message,
    }


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
        if t == "reply":
            content = raw.get("content") or []
            target_obj = raw.get("target")
            return ReplyAction(
                content=content if isinstance(content, list) else [],
                target=target_obj if isinstance(target_obj, dict) else None,
                related_msg_hashes=_as_str_list(raw.get("related_msg_hashes")),
            )
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

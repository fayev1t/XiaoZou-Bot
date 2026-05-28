"""Projector — build DecisionContext from the agent_events stream.

Contract: 开发文档/v2.0/任务与决策契约.md §2.3, §4.2, §5.1, §5.2

Strategy:
- Fetch agent_visible events for this scope within a lookback window.
- Fold `agent.task_*` into TaskView snapshots (active_tasks).
- Pair `agent.tool_called` with `agent.tool_result | agent.tool_failed`
  into ToolResultView; only completed pairs (status != pending) end up in
  `pending_tool_results`.
- Build the timeline from messages / notices / tool-call pairs / replies /
  agent-visible runtime hints. Task and tool-result events are folded
  upstream and do NOT produce timeline rows of their own.

Folding and rendering are split into pure staticmethods so unit tests
can drive them without a DB.

Renderers emit a compact XML envelope. Each renderer:
- Properly escapes user-supplied content (`<`, `>`, `&`, `"`) so chat
  messages cannot inject pseudo-tags into the LLM context.
- Renders timestamps as full ISO-8601 with timezone (cross-day events
  distinguishable).
- Walks OneBot V11 segments structurally (at / reply / image / face /
  poke / record / video / share / forward / ...) instead of dumping the
  raw CQ-code string — see `_render_segments` for the per-type contract.
- Serializes dict/list values as JSON (not Python repr).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qqbot.core.logging import get_logger
from qqbot.models.agent_event import AgentEvent
from qqbot.services.agent_loop.decision import (
    DecisionContext,
    ImageRef,
    ProgressNote,
    TaskView,
    TimelineItem,
    ToolResultView,
)
from qqbot.services.agent_loop.event_writer import parse_scope_key

logger = get_logger(__name__)

SessionFactory = Callable[[], AsyncSession]


@dataclass(frozen=True)
class _EventSnapshot:
    """Minimal row representation: avoids leaking the SQLAlchemy ORM into
    folding logic and lets tests build fixtures with plain dataclasses."""

    event_id: str
    occurred_at: datetime
    origin: str
    type: str
    scope: str
    group_id: int | None
    user_id: int | None
    visibility: str
    correlation_id: str | None
    causation_id: str | None
    payload: dict


def _snapshot_from_row(row: AgentEvent) -> _EventSnapshot:
    return _EventSnapshot(
        event_id=row.event_id,
        occurred_at=row.occurred_at,
        origin=row.origin,
        type=row.type,
        scope=row.scope,
        group_id=row.group_id,
        user_id=row.user_id,
        visibility=row.visibility,
        correlation_id=row.correlation_id,
        causation_id=row.causation_id,
        payload=dict(row.payload or {}),
    )


class Projector:
    # 单条 tool_result 渲染上限：超过即截断尾部并加 <truncated/>。websearch
    # 等工具的 results 列表很容易爆掉 prompt token，必须兜底。
    MAX_TOOL_RESULT_CHARS = 2048

    # 单 task 折叠时保留的最近进度笔记条数。LLM 关心的是最近"我自己想到啥"，
    # 老笔记换 token 不划算。
    MAX_PROGRESS_NOTES_PER_TASK = 5

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        lookback_hours: int = 24,
        max_items: int = 300,
        max_timeline_items: int = 100,
    ) -> None:
        self._session_factory = session_factory
        self._lookback = timedelta(hours=lookback_hours)
        # 拉取 fetch 上限保持较大，给 fold_tasks / fold_tool_results 喂够事件；
        # 真正塞给 LLM 的 timeline 在 project() 里再裁到 max_timeline_items 条。
        self._max_items = max_items
        self._max_timeline_items = max_timeline_items

    async def build_context(
        self,
        *,
        scope_key: str,
        correlation_id: str,
        tick_seq: int,
        now: datetime,
    ) -> DecisionContext:
        scope, group_id, _ = parse_scope_key(scope_key)
        cutoff = now - self._lookback

        events = await self._fetch(scope, group_id, cutoff)
        return self.project(
            events,
            scope_key=scope_key,
            correlation_id=correlation_id,
            tick_seq=tick_seq,
            now=now,
            max_timeline_items=self._max_timeline_items,
        )

    async def _fetch(
        self,
        scope: str,
        group_id: int | None,
        cutoff: datetime,
    ) -> list[_EventSnapshot]:
        stmt = (
            select(AgentEvent)
            .where(AgentEvent.scope == scope)
            .where(AgentEvent.visibility == "agent_visible")
            .where(AgentEvent.occurred_at >= cutoff)
        )
        if scope == "group" and group_id is not None:
            stmt = stmt.where(AgentEvent.group_id == group_id)
        stmt = stmt.order_by(AgentEvent.occurred_at.desc()).limit(self._max_items)

        async with self._session_factory() as session:
            result = await session.execute(stmt)
            rows = result.scalars().all()
        # Reverse to chronological order for downstream folding.
        return [_snapshot_from_row(row) for row in reversed(rows)]

    # ─── Pure projection: testable without DB ───

    @staticmethod
    def project(
        events: Sequence[_EventSnapshot],
        *,
        scope_key: str,
        correlation_id: str,
        tick_seq: int,
        now: datetime,
        max_timeline_items: int | None = None,
    ) -> DecisionContext:
        active_tasks = Projector.fold_tasks(events, scope_key=scope_key)
        tool_views = Projector.fold_tool_results(events)
        pending_tool_results = [
            tv for tv in tool_views if tv.status != "pending"
        ]
        timeline = Projector.build_timeline(events, tool_views=tool_views)
        # 裁到尾部 max_timeline_items 条 —— fetch 上限给得宽是为了 fold 任务/
        # 工具结果时能看到足够长的事件链，但塞给 LLM 的不必那么多。
        if max_timeline_items is not None and len(timeline) > max_timeline_items:
            timeline = timeline[-max_timeline_items:]
        return DecisionContext(
            scope_key=scope_key,
            correlation_id=correlation_id,
            tick_seq=tick_seq,
            now=now,
            timeline=timeline,
            active_tasks=active_tasks,
            pending_tool_results=pending_tool_results,
        )

    # ─── Folding helpers ───

    @staticmethod
    def fold_tasks(
        events: Iterable[_EventSnapshot], *, scope_key: str
    ) -> list[TaskView]:
        """Pending / running TaskView list. done/failed are dropped (kept
        only as historical events; the LLM sees them via task_* but not in
        active_tasks)."""
        tasks: dict[str, dict] = {}
        for ev in events:
            if ev.type == "agent.task_created":
                tid = ev.payload.get("task_id")
                if not tid:
                    continue
                tasks[tid] = {
                    "task_id": tid,
                    "scope_key": scope_key,
                    "description": ev.payload.get("description", ""),
                    "related_tools": list(ev.payload.get("related_tools") or []),
                    "parent_task_id": ev.payload.get("parent_task_id"),
                    "state": "pending",
                    "created_at": ev.occurred_at,
                    "last_changed_at": ev.occurred_at,
                    "last_change_reason": None,
                    "pending_tool_call_ids": [],
                    "triggered_by_event_id": ev.payload.get("triggered_by_event_id"),
                    "progress_notes": [],
                }
            elif ev.type == "agent.task_state_changed":
                tid = ev.payload.get("task_id")
                if not tid or tid not in tasks:
                    continue
                tasks[tid]["state"] = ev.payload.get(
                    "to_state", tasks[tid]["state"]
                )
                tasks[tid]["last_changed_at"] = ev.occurred_at
                tasks[tid]["last_change_reason"] = ev.payload.get("reason")
            elif ev.type == "agent.task_progress_noted":
                tid = ev.payload.get("task_id")
                note = ev.payload.get("note")
                if not tid or tid not in tasks or not note:
                    continue
                tasks[tid]["progress_notes"].append(
                    ProgressNote(at=ev.occurred_at, note=str(note))
                )
            elif ev.type == "agent.tool_called":
                tid = ev.payload.get("task_id")
                tc_id = ev.payload.get("tool_call_id")
                if tid and tid in tasks and tc_id:
                    tasks[tid]["pending_tool_call_ids"].append(tc_id)
            elif ev.type in ("agent.tool_result", "agent.tool_failed"):
                tc_id = ev.payload.get("tool_call_id")
                for t in tasks.values():
                    if tc_id in t["pending_tool_call_ids"]:
                        t["pending_tool_call_ids"].remove(tc_id)

        # 每个 task 保留最近 N 条进度笔记 —— 时间顺序由 event stream 保证。
        for t in tasks.values():
            notes = t["progress_notes"]
            if len(notes) > Projector.MAX_PROGRESS_NOTES_PER_TASK:
                t["progress_notes"] = notes[-Projector.MAX_PROGRESS_NOTES_PER_TASK:]

        return [
            TaskView(**d)
            for d in tasks.values()
            if d["state"] in ("pending", "running")
        ]

    @staticmethod
    def fold_tool_results(
        events: Iterable[_EventSnapshot],
    ) -> list[ToolResultView]:
        """All tool calls in window, paired with their result/failure if any."""
        calls: dict[str, dict] = {}
        for ev in events:
            if ev.type == "agent.tool_called":
                tc_id = ev.payload.get("tool_call_id")
                if not tc_id:
                    continue
                calls[tc_id] = {
                    "tool_call_id": tc_id,
                    "tool_name": ev.payload.get("tool_name", ""),
                    "arguments": dict(ev.payload.get("arguments") or {}),
                    "status": "pending",
                    "result": None,
                    "error_kind": None,
                    "error_message": None,
                }
            elif ev.type == "agent.tool_result":
                tc_id = ev.payload.get("tool_call_id")
                if tc_id in calls:
                    calls[tc_id]["status"] = "succeeded"
                    calls[tc_id]["result"] = ev.payload.get("result")
            elif ev.type == "agent.tool_failed":
                tc_id = ev.payload.get("tool_call_id")
                if tc_id in calls:
                    calls[tc_id]["status"] = "failed"
                    calls[tc_id]["error_kind"] = ev.payload.get("error_kind")
                    calls[tc_id]["error_message"] = ev.payload.get("error_message")
        return [ToolResultView(**d) for d in calls.values()]

    @staticmethod
    def build_timeline(
        events: Sequence[_EventSnapshot],
        *,
        tool_views: Sequence[ToolResultView],
    ) -> list[TimelineItem]:
        tool_view_by_id = {tv.tool_call_id: tv for tv in tool_views}
        # 预扫一遍构建 reply 段引用所需的索引（被回复消息摘要 + 用户名映射），
        # 让单条消息渲染时无需再遍历全部事件。
        excerpt_by_msg_id = _build_excerpt_index(events)
        name_by_user_id = _build_user_name_index(events)

        items: list[TimelineItem] = []
        for ev in events:
            if ev.type.startswith("agent.task_"):
                # folded into active_tasks
                continue
            if ev.type in ("agent.tool_result", "agent.tool_failed"):
                # rendered alongside the matching tool_called row
                continue
            if ev.type in (
                "agent.reply_delivered",
                "agent.reply_failed",
                "agent.idle_decision",
                "agent.decision_emitted",
            ):
                # decision/idle/delivered are operational — keep them out
                # of the prompt by default to avoid context bloat.
                continue

            if ev.type == "agent.tool_called":
                tc_id = ev.payload.get("tool_call_id")
                tv = tool_view_by_id.get(tc_id)
                items.append(
                    TimelineItem(
                        event_id=ev.event_id,
                        occurred_at=ev.occurred_at,
                        kind="tool_call",
                        render=Projector._render_tool_call(ev, tv),
                        related_event_ids=[],
                    )
                )
            elif ev.type == "agent.reply_emitted":
                render, images = Projector._render_reply(
                    ev, excerpt_by_msg_id, name_by_user_id
                )
                items.append(
                    TimelineItem(
                        event_id=ev.event_id,
                        occurred_at=ev.occurred_at,
                        kind="agent_reply",
                        render=render,
                        images=images,
                    )
                )
            elif ev.type.startswith("external.message."):
                render, images = Projector._render_message(
                    ev, excerpt_by_msg_id, name_by_user_id
                )
                items.append(
                    TimelineItem(
                        event_id=ev.event_id,
                        occurred_at=ev.occurred_at,
                        kind="message",
                        render=render,
                        images=images,
                    )
                )
            elif ev.type.startswith("external.notice."):
                items.append(
                    TimelineItem(
                        event_id=ev.event_id,
                        occurred_at=ev.occurred_at,
                        kind="notice",
                        render=Projector._render_notice(ev),
                    )
                )
            elif ev.type.startswith("runtime.") and ev.visibility == "agent_visible":
                items.append(
                    TimelineItem(
                        event_id=ev.event_id,
                        occurred_at=ev.occurred_at,
                        kind="system_hint",
                        render=Projector._render_runtime(ev),
                    )
                )
            # silently drop anything else
        return items

    # ─── Renderers ───

    @staticmethod
    def _render_message(
        ev: _EventSnapshot,
        excerpt_by_msg_id: dict[str, str],
        name_by_user_id: dict[str, str],
    ) -> tuple[str, list[ImageRef]]:
        sender = ev.payload.get("sender") or {}
        name = sender.get("card") or sender.get("nickname") or "?"
        qq = sender.get("user_id") or ev.user_id or "?"
        msg_id = ev.payload.get("onebot_message_id") or ""
        time_str = ev.occurred_at.isoformat(timespec="seconds")

        segments = ev.payload.get("segments") or []
        body, images = _render_segments(
            segments, excerpt_by_msg_id, name_by_user_id
        )
        # raw_message 兜底：mapper 上游异常时 segments 可能为空但 raw_message 还在
        if not body:
            raw = ev.payload.get("raw_message", "")
            if raw:
                body = _esc_text(str(raw))

        attrs = [
            f'sender="{_esc_attr(str(name))}({qq})"',
            f'at="{time_str}"',
        ]
        if msg_id:
            attrs.append(f'id="{_esc_attr(str(msg_id))}"')
        return f"<message {' '.join(attrs)}>{body}</message>", images

    @staticmethod
    def _render_notice(ev: _EventSnapshot) -> str:
        kind = ev.type.replace("external.notice.", "")
        attrs = [f'kind="{_esc_attr(kind)}"']
        sub = ev.payload.get("sub_type")
        if sub:
            attrs.append(f'sub_type="{_esc_attr(str(sub))}"')
        if ev.user_id is not None:
            attrs.append(f'user="{ev.user_id}"')
        op = ev.payload.get("operator_id")
        if op:
            attrs.append(f'operator="{op}"')
        target = ev.payload.get("target_id")
        if target:
            attrs.append(f'target="{target}"')
        attrs.append(f'at="{ev.occurred_at.isoformat(timespec="seconds")}"')
        return f"<notice {' '.join(attrs)}/>"

    @staticmethod
    def _render_tool_call(
        ev: _EventSnapshot, tv: ToolResultView | None
    ) -> str:
        name = str(ev.payload.get("tool_name", "?"))
        args = ev.payload.get("arguments", {})
        args_json = _safe_json(args)
        if tv is None or tv.status == "pending":
            inner = "<pending/>"
            status = "pending"
        elif tv.status == "succeeded":
            result_json = _safe_json(tv.result)
            truncated = ""
            if len(result_json) > Projector.MAX_TOOL_RESULT_CHARS:
                result_json = result_json[: Projector.MAX_TOOL_RESULT_CHARS]
                truncated = "<truncated/>"
            inner = f"<result>{_esc_text(result_json)}{truncated}</result>"
            status = "succeeded"
        else:
            inner = (
                f'<error kind="{_esc_attr(str(tv.error_kind or ""))}">'
                f"{_esc_text(str(tv.error_message or ''))}</error>"
            )
            status = "failed"
        return (
            f'<tool-call name="{_esc_attr(name)}" status="{status}">'
            f"<args>{_esc_text(args_json)}</args>{inner}</tool-call>"
        )

    @staticmethod
    def _render_reply(
        ev: _EventSnapshot,
        excerpt_by_msg_id: dict[str, str],
        name_by_user_id: dict[str, str],
    ) -> tuple[str, list[ImageRef]]:
        content = ev.payload.get("content") or []
        body, images = _render_segments(
            content, excerpt_by_msg_id, name_by_user_id
        )
        time_str = ev.occurred_at.isoformat(timespec="seconds")
        return f'<agent-reply at="{time_str}">{body}</agent-reply>', images

    @staticmethod
    def _render_runtime(ev: _EventSnapshot) -> str:
        kind = ev.type.replace("runtime.", "")
        payload_json = _safe_json(ev.payload)
        return (
            f'<system-hint kind="{_esc_attr(kind)}">'
            f"{_esc_text(payload_json)}</system-hint>"
        )


# ─── XML escape + JSON helpers ───


def _esc_text(s: str) -> str:
    return (
        s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def _esc_attr(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _safe_json(value) -> str:
    """JSON 序列化，不可序列化的对象用 str() 兜底，避免 prompt 渲染崩溃。"""
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return str(value)


# ─── Segment-level rendering ───


def _render_segments(
    segments: Iterable,
    excerpt_by_msg_id: dict[str, str],
    name_by_user_id: dict[str, str],
) -> tuple[str, list[ImageRef]]:
    """把 OneBot V11 段数组翻译成内联 XML 标签 + 收集已落盘的 ImageRef。

    支持的段类型 → 标签：
      text     → 原文（XML escape）
      at       → <at user="..." name="..."/> 或 <at-all/>
      reply    → <reply to="msg_id" excerpt="..."/>（excerpt 在 timeline 内可查时）
      image    → <image hash="<sha256>"/> 或 <image/>（无 hash 或下载失败）
      face     → <face id="N"/>
      record   → <voice/>            (LLM 当前不消费语音内容)
      video    → <video/>            (同上)
      poke     → <poke target="qq"/>
      forward  → <forward id="..."/>
      json/xml/share → <card type="..."/>
      其他     → <misc type="..."/>

    image segment 的富化字段 (file_hash / local_path / mime / downloaded)
    由 event_ingest/media.py 写在 segment 顶层（不在 data 内），见
    EventIngest契约.md §6.1。downloaded=true 且 local_path 存在的图片
    才会进 images 列表，供 llm_planner 后续读 bytes 拼 multimodal block。
    """
    parts: list[str] = []
    images: list[ImageRef] = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        t = seg.get("type")
        d = seg.get("data") or {}
        if t == "text":
            parts.append(_esc_text(str(d.get("text", ""))))
        elif t == "at":
            qq = str(d.get("qq", "")).strip()
            if qq == "all":
                parts.append("<at-all/>")
            elif qq:
                nm = name_by_user_id.get(qq)
                if nm:
                    parts.append(
                        f'<at user="{_esc_attr(qq)}" name="{_esc_attr(nm)}"/>'
                    )
                else:
                    parts.append(f'<at user="{_esc_attr(qq)}"/>')
            else:
                parts.append("<at/>")
        elif t == "reply":
            rid = str(d.get("id", "")).strip()
            excerpt = excerpt_by_msg_id.get(rid) if rid else None
            if rid and excerpt:
                parts.append(
                    f'<reply to="{_esc_attr(rid)}" '
                    f'excerpt="{_esc_attr(excerpt)}"/>'
                )
            elif rid:
                parts.append(f'<reply to="{_esc_attr(rid)}"/>')
            else:
                parts.append("<reply/>")
        elif t == "image":
            # 富化字段写在 segment 顶层（media.py 契约），不在 data 内
            file_hash = seg.get("file_hash")
            if file_hash:
                parts.append(f'<image hash="{_esc_attr(str(file_hash))}"/>')
                if seg.get("downloaded") and seg.get("local_path"):
                    images.append(
                        ImageRef(
                            file_hash=str(file_hash),
                            local_path=str(seg["local_path"]),
                            mime=str(seg.get("mime") or "image/png"),
                        )
                    )
            else:
                parts.append("<image/>")
        elif t == "face":
            fid = str(d.get("id", "")).strip()
            if fid:
                parts.append(f'<face id="{_esc_attr(fid)}"/>')
            else:
                parts.append("<face/>")
        elif t == "record":
            parts.append("<voice/>")
        elif t == "video":
            parts.append("<video/>")
        elif t == "poke":
            target = d.get("qq") or d.get("user_id")
            if target:
                parts.append(f'<poke target="{_esc_attr(str(target))}"/>')
            else:
                parts.append("<poke/>")
        elif t == "forward":
            fid = str(d.get("id", "")).strip()
            if fid:
                parts.append(f'<forward id="{_esc_attr(fid)}"/>')
            else:
                parts.append("<forward/>")
        elif t in ("json", "xml", "share"):
            parts.append(f'<card type="{_esc_attr(str(t))}"/>')
        else:
            parts.append(f'<misc type="{_esc_attr(str(t or "unknown"))}"/>')
    return "".join(parts), images


def _build_excerpt_index(events: Iterable[_EventSnapshot]) -> dict[str, str]:
    """timeline 内 onebot_message_id → 文本摘要（前 40 字）。

    用于渲染 reply 段时给 LLM 提供"被回复消息说了啥"的上下文；命中不到
    （消息在 lookback 窗口外或被 napcat 抛弃了）就只渲染 reply.to 不带
    excerpt。
    """
    out: dict[str, str] = {}
    for ev in events:
        if not ev.type.startswith("external.message."):
            continue
        mid = ev.payload.get("onebot_message_id")
        if not mid:
            continue
        segs = ev.payload.get("segments") or []
        text_parts: list[str] = []
        for seg in segs:
            if not isinstance(seg, dict):
                continue
            if seg.get("type") == "text":
                text_parts.append(str((seg.get("data") or {}).get("text", "")))
        excerpt = "".join(text_parts).strip()
        if not excerpt:
            # 无文本段时（纯图 / 纯表情）给 segment 摘要
            tags = [s.get("type") for s in segs if isinstance(s, dict)]
            if tags:
                excerpt = "[" + ", ".join(tags) + "]"
        if len(excerpt) > 40:
            excerpt = excerpt[:40] + "…"
        out[str(mid)] = excerpt
    return out


def _build_user_name_index(
    events: Iterable[_EventSnapshot],
) -> dict[str, str]:
    """user_id → 最近一次出现的 card/nickname。用于给 at 段添加名字。"""
    out: dict[str, str] = {}
    for ev in events:
        if not ev.type.startswith("external.message."):
            continue
        sender = ev.payload.get("sender") or {}
        uid = sender.get("user_id") or ev.user_id
        if uid is None:
            continue
        name = sender.get("card") or sender.get("nickname")
        if name:
            out[str(uid)] = str(name)
    return out

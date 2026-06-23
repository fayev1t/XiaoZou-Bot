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
from qqbot.core.time import CHINA_TIMEZONE
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
    # asyncpg 把 TIMESTAMPTZ 列硬编码返回 UTC tzinfo（与 PG session
    # timezone 设置无关）。但人类可读输出必须用北京时间——这是项目契约：
    # 写入侧 china_now() 已是 +08:00，读出侧必须 normalize 回去，否则
    # timeline 渲染给 LLM 时会出现 "+00:00" 这种和数据库语义不一致的尾巴，
    # LLM 容易被它带歪（"现在凌晨1点，用户应该睡了" 实际是早上 9 点）。
    occurred_at = row.occurred_at
    if occurred_at is not None and occurred_at.tzinfo is not None:
        occurred_at = occurred_at.astimezone(CHINA_TIMEZONE)
    return _EventSnapshot(
        event_id=row.event_id,
        occurred_at=occurred_at,
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
        bot_user_id: str | None = None,
    ) -> DecisionContext:
        scope, group_id, _ = parse_scope_key(scope_key)
        cutoff = now - self._lookback

        events = await self._fetch(scope, group_id, cutoff)
        # bot_role 单独一次 SQL 查 —— runtime.bot_role_observed 可能远早于
        # lookback 窗口（比如启动 sweep 跑过一次就再没变），不应受 cutoff 影响。
        bot_role: str | None = None
        if scope == "group" and group_id is not None:
            bot_role = await self._fetch_latest_bot_role(group_id, bot_user_id)
        return self.project(
            events,
            scope_key=scope_key,
            correlation_id=correlation_id,
            tick_seq=tick_seq,
            now=now,
            max_timeline_items=self._max_timeline_items,
            bot_user_id=bot_user_id,
            bot_role=bot_role,
        )

    async def _fetch_latest_bot_role(
        self,
        group_id: int,
        bot_user_id: str | None,
    ) -> str | None:
        """查该群最新一条 runtime.bot_role_observed。

        不走 _fetch 的 lookback 窗口与 agent_visible 过滤：
        - lookback：bot 角色可能很久不变，sweep 后几个月才有 group_admin 事件触发
          下一次写入，硬等窗口会让 bot_role 在 lookback 推进时凭空消失。
        - visibility：runtime.bot_role_observed 默认 agent_visible，但即使未来
          调成 runtime_only 也应能取到——这是事实数据，不是给 LLM 的渲染数据。

        ``bot_user_id`` 用来在多账号场景下只取本 bot 自己的 baseline；为 None
        时不过滤 self_id（单 bot 场景 / 启动初期 bot_registry 还空）。
        """
        from sqlalchemy import desc

        stmt = (
            select(AgentEvent)
            .where(AgentEvent.type == "runtime.bot_role_observed")
            .where(AgentEvent.scope == "group")
            .where(AgentEvent.group_id == group_id)
            .order_by(desc(AgentEvent.occurred_at))
            .limit(10)  # 取最近 10 条，应用层按 self_id 过滤后取首条
        )
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            rows = list(result.scalars().all())
        for row in rows:
            payload = row.payload or {}
            self_id = payload.get("self_id")
            if bot_user_id is None or self_id is None or str(self_id) == bot_user_id:
                role = payload.get("role")
                if isinstance(role, str) and role.strip():
                    return role.strip().lower()
        return None

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
        bot_user_id: str | None = None,
        bot_role: str | None = None,
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
        # 如果 caller 没单独传 bot_role（pure project() 测试常常如此），尝试从
        # 事件列表里 fold 一次——支持纯函数测试不需要 DB 也能验证 fold 逻辑。
        if bot_role is None:
            bot_role = Projector.fold_bot_role(
                events, bot_user_id=bot_user_id
            )
        # 类型上 DecisionContext.bot_role 是 Literal[...]，但跑期我们对未知值
        # 一律 None（防止 LLM 拿到"垃圾角色字符串"做判断）。
        normalized_role: str | None = None
        if isinstance(bot_role, str):
            low = bot_role.strip().lower()
            if low in ("owner", "admin", "member"):
                normalized_role = low
        return DecisionContext(
            scope_key=scope_key,
            correlation_id=correlation_id,
            tick_seq=tick_seq,
            now=now,
            timeline=timeline,
            active_tasks=active_tasks,
            pending_tool_results=pending_tool_results,
            bot_user_id=bot_user_id,
            bot_role=normalized_role,  # type: ignore[arg-type]
        )

    @staticmethod
    def fold_bot_role(
        events: Iterable[_EventSnapshot],
        *,
        bot_user_id: str | None = None,
    ) -> str | None:
        """Pure fold: 从事件序列里取最新一条 runtime.bot_role_observed.role。

        多账号过滤：payload.self_id 必须等于 bot_user_id；bot_user_id 为 None
        时不过滤（单 bot 部署）。事件被假设为升序排列（与 _fetch 返回一致），
        因此最后一条匹配即为"最新"。
        """
        latest_role: str | None = None
        for ev in events:
            if ev.type != "runtime.bot_role_observed":
                continue
            payload = ev.payload or {}
            self_id = payload.get("self_id")
            if bot_user_id is not None and self_id is not None and str(self_id) != bot_user_id:
                continue
            role = payload.get("role")
            if isinstance(role, str) and role.strip():
                latest_role = role.strip().lower()
        return latest_role

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
        # author_by_msg_id：被回复消息的作者 "昵称(QQ)"。reply 段渲染时据此标
        # from=，让 LLM 一眼看清"是 B 在引用某人"而不是"某人在发言"——这是
        # addressee 误判（把别人引用你当成你说话）的根因修复。覆盖外部消息 +
        # bot 自己已投递的 reply（后者标 from="我(self_id)"，与 bot_user_id 比对
        # 即知"别人引用的是你自己的发言"）。
        author_by_msg_id = _build_author_index(events)

        items: list[TimelineItem] = []
        for ev in events:
            if ev.type.startswith("agent.task_"):
                # folded into active_tasks
                continue
            if ev.type in ("agent.tool_result", "agent.tool_failed"):
                # rendered alongside the matching tool_called row
                continue
            if ev.type in (
                "agent.reply_emitted",
                "agent.reply_delivered",
                "agent.reply_failed",
                "agent.idle_decision",
                "agent.decision_emitted",
            ):
                # 发言统一表示为 reply 工具的 <tool-call name="reply"> 行（架构
                # 一致性：reply 是工具，与 websearch 等同构），不再单独渲染
                # <agent-reply>。reply_emitted 仍写入事件流供 ReplySendWorker
                # 消费、并经 reply_delivered 折出 author/quote 信息，只是它本身
                # 不进 timeline。delivered/failed/decision/idle 同为运营事件，
                # 不进 prompt 以免 context 膨胀。
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
            elif ev.type.startswith("external.message."):
                render, images = Projector._render_message(
                    ev, excerpt_by_msg_id, name_by_user_id, author_by_msg_id
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
        author_by_msg_id: dict[str, str] | None = None,
    ) -> tuple[str, list[ImageRef]]:
        sender = ev.payload.get("sender") or {}
        name = sender.get("card") or sender.get("nickname") or "?"
        qq = sender.get("user_id") or ev.user_id or "?"
        msg_id = ev.payload.get("onebot_message_id") or ""
        time_str = ev.occurred_at.isoformat(timespec="seconds")

        segments = ev.payload.get("segments") or []
        body, images = _render_segments(
            segments, excerpt_by_msg_id, name_by_user_id, author_by_msg_id
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
    author_by_msg_id: dict[str, str] | None = None,
) -> tuple[str, list[ImageRef]]:
    """把 OneBot V11 段数组翻译成内联 XML 标签 + 收集已落盘的 ImageRef。

    支持的段类型 → 标签：
      text     → 原文（XML escape）
      at       → <at user="..." name="..."/> 或 <at-all/>
      reply    → <reply to="msg_id" from="昵称(QQ)" excerpt="..."/>
                 （from= 标注被引用消息的作者，author_by_msg_id 命中时；
                  excerpt= 在 timeline 内可查时）
      image    → <image hash="<sha256>"/> 或 <image/>（无 hash 或下载失败）
      face     → <face id="N"/>          (QQ 原生黄豆表情)
      mface    → <mface summary="[赞]"/>  (商城/魔法表情，动图贴纸)
      record   → <voice/>            (LLM 当前不消费语音内容)
      video    → <video/>            (同上)
      file     → <file name="..."/>  (聊天文件)
      poke     → <poke target="qq"/>
      dice     → <dice value="N"/>   (掷骰子结果 1-6)
      rps      → <rps value="N"/>    (猜拳 1=石头 2=剪刀 3=布)
      markdown → <markdown/>         (内容不展开)
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
            if rid:
                attrs = [f'to="{_esc_attr(rid)}"']
                # from=：被引用消息的作者。这是"别人引用我 ≠ 我在发言"的关键——
                # 没有它，LLM 看到被引用内容内联在画面里，容易当成对方刚说的话。
                author = (author_by_msg_id or {}).get(rid)
                if author:
                    attrs.append(f'from="{_esc_attr(author)}"')
                excerpt = excerpt_by_msg_id.get(rid)
                if excerpt:
                    attrs.append(f'excerpt="{_esc_attr(excerpt)}"')
                parts.append(f'<reply {" ".join(attrs)}/>')
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
        elif t == "mface":
            # 商城/魔法表情（动图贴纸）。summary 是人类可读释义（如 "[羡慕]"），
            # 是 LLM 唯一能理解的语义；缺失时退化为无属性 <mface/>。
            summary = str(d.get("summary", "")).strip()
            if summary:
                parts.append(f'<mface summary="{_esc_attr(summary)}"/>')
            else:
                parts.append("<mface/>")
        elif t == "record":
            parts.append("<voice/>")
        elif t == "video":
            parts.append("<video/>")
        elif t == "file":
            fname = str(d.get("name", "") or d.get("file", "")).strip()
            if fname:
                parts.append(f'<file name="{_esc_attr(fname)}"/>')
            else:
                parts.append("<file/>")
        elif t == "poke":
            target = d.get("qq") or d.get("user_id")
            if target:
                parts.append(f'<poke target="{_esc_attr(str(target))}"/>')
            else:
                parts.append("<poke/>")
        elif t == "dice":
            val = str(d.get("result", "") or d.get("value", "")).strip()
            parts.append(f'<dice value="{_esc_attr(val)}"/>' if val else "<dice/>")
        elif t == "rps":
            # 猜拳：napcat result 1=石头 2=剪刀 3=布
            val = str(d.get("result", "") or d.get("value", "")).strip()
            parts.append(f'<rps value="{_esc_attr(val)}"/>' if val else "<rps/>")
        elif t == "markdown":
            parts.append("<markdown/>")
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


def _build_reply_msgid_index(
    events: Iterable[_EventSnapshot],
) -> dict[str, str]:
    """reply_id → onebot_message_id，从 agent.reply_delivered 折出。

    reply 工具写 agent.reply_emitted（带 reply_id），ReplySendWorker 投递成功后
    写 agent.reply_delivered（带同一 reply_id + napcat 分配的 onebot_message_id）。
    这里把两者对上，供 _build_author_index 给 bot 自己已发出的消息标 from=，
    从而让别人 <reply to="MSG_ID"> 引用 bot 时，reply 段能渲染 from="我(self_id)"，
    LLM 据此判定"这条是在回复我"。（发言本身的 timeline 表示走 reply 工具的
    <tool-call name="reply">，不再有独立的 <agent-reply>。）
    """
    out: dict[str, str] = {}
    for ev in events:
        if ev.type != "agent.reply_delivered":
            continue
        payload = ev.payload or {}
        reply_id = payload.get("reply_id")
        msgid = payload.get("onebot_message_id")
        if reply_id and msgid is not None:
            out[str(reply_id)] = str(msgid)
    return out


def _build_author_index(
    events: Iterable[_EventSnapshot],
) -> dict[str, str]:
    """onebot_message_id → 作者标签 "昵称(QQ)"。用于 reply 段标 from=。

    覆盖两类来源：
    - 外部消息：作者 = sender 的 card/nickname + user_id。别人引用某人时，
      LLM 据此判断被引用的是谁（含群主自己）——而不是误以为那人在发言。
    - bot 自己已投递的 reply：作者标 "我(self_id)"，onebot_message_id 来自
      配对的 reply_delivered。这样别人引用 bot 时 from= 的 QQ 命中 bot_user_id，
      LLM 用同一条规则（比对 from 的 QQ）即可判定"被引用的是我自己"。
    """
    out: dict[str, str] = {}
    delivered_msgid_by_reply_id = _build_reply_msgid_index(events)
    reply_self_id_by_reply_id: dict[str, str | None] = {}
    for ev in events:
        if ev.type == "agent.reply_delivered":
            payload = ev.payload or {}
            reply_id = payload.get("reply_id")
            if reply_id:
                reply_self_id_by_reply_id[str(reply_id)] = payload.get("self_id")
        if not ev.type.startswith("external.message."):
            continue
        mid = ev.payload.get("onebot_message_id")
        if not mid:
            continue
        sender = ev.payload.get("sender") or {}
        name = sender.get("card") or sender.get("nickname") or "?"
        qq = sender.get("user_id") or ev.user_id
        out[str(mid)] = f"{name}({qq})" if qq is not None else str(name)
    # bot 自己的发言：reply_id → onebot_message_id（delivered）+ self_id。
    for reply_id, msgid in delivered_msgid_by_reply_id.items():
        self_id = reply_self_id_by_reply_id.get(reply_id)
        out[msgid] = f"我({self_id})" if self_id else "我"
    return out

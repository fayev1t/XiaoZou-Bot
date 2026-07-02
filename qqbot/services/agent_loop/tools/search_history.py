"""SearchHistoryTool — v2 自带的历史事件检索工具。

设计动机（任务与决策契约 §动态记忆检索；拓扑 README §5.3 关联）：

  Projector 每 tick 只把尾部 100 条 timeline 喂给 LLM——这是控制 prompt
  长度的硬上限。当 LLM 在某个 task 推进过程中需要更早的上下文（"前天某某
  说了啥"），它通过这个工具按需检索而不是被动等被裁掉的事件回流。

三种过滤方式同时支持（逻辑 AND）：
  1. 锚点（anchor）：anchor_event_id 直接传，或传 task_id 让工具去查
     agent.task_created.payload.triggered_by_event_id 作为锚。查询只看
     anchor 之前发生的事件（ULID 字典序天然=时间序）
  2. 时间窗：start_time / end_time（ISO8601 字符串）
  3. 关键字：query 在 payload->>'raw_message' 上 ILIKE 子串匹配

返回结构复用 Projector 渲染器，与正向 timeline 完全同构。

错误策略：
  - scope_key 缺失 / 非法 → return ToolOutcome.failure(invalid_arguments)（不 raise）
  - 锚点 task 查不到 / triggered_by_event_id 缺失 → 加 warning，不报错
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select

from qqbot.core.logging import get_logger
from qqbot.core.time import normalize_china_time
from qqbot.models.agent_event import AgentEvent
from qqbot.services.agent_loop.event_writer import parse_scope_key
from qqbot.services.agent_loop.projection import Projector, _snapshot_from_row
from qqbot.services.agent_loop.prompts import load_sibling_md
from qqbot.services.agent_loop.tool_registry import BaseTool, ToolOutcome

logger = get_logger(__name__)

_DEFAULT_LIMIT = 20
_MAX_LIMIT = 50

_USAGE_PROMPT = load_sibling_md(__file__, "search_history.md")


class SearchHistoryTool(BaseTool):
    """实现 Tool 协议。session_factory 从 run() 的 context 进（ToolWorker
    统一注入），无构造依赖 —— 与 websearch / send_message 同构。
    """

    name = "search_history"
    description = (
        "Retrieve older events from this scope's history beyond the current "
        "timeline window (which is capped at 100 most-recent items). Use this "
        "when answering a question requires context older than what you can "
        "see directly. Filters can combine: anchor (anchor_event_id or task_id "
        "to use that task's trigger event as anchor), a time window "
        "(start_time/end_time ISO8601), and/or a keyword (query — substring "
        "match against message text). Results are returned in the same XML "
        "format as the normal timeline."
    )
    usage_prompt = _USAGE_PROMPT
    # required_permission / required_bot_role 用 BaseTool 默认值（GUEST /
    # 不限 bot 角色）：查历史属于内部知识检索，任何群员都能让小奏查，无需管理员。
    arguments_schema = {
        "type": "object",
        "properties": {
            "anchor_event_id": {
                "type": "string",
                "description": (
                    "If set, only events strictly older than this event_id "
                    "are returned (event_id is ULID = chronologically sortable)."
                ),
            },
            "task_id": {
                "type": "string",
                "description": (
                    "If set and anchor_event_id is not, resolve the anchor "
                    "to this task's triggered_by_event_id."
                ),
            },
            "start_time": {
                "type": "string",
                "description": "ISO8601 lower bound (inclusive).",
            },
            "end_time": {
                "type": "string",
                "description": "ISO8601 upper bound (inclusive).",
            },
            "query": {
                "type": "string",
                "description": (
                    "Substring to search in message text. Case-insensitive."
                ),
            },
            "limit": {
                "type": "integer",
                "description": f"Max items to return; capped at {_MAX_LIMIT}.",
                "default": _DEFAULT_LIMIT,
            },
        },
    }

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        # GUEST + 不限 scope：enforce_access 实为 no-op，但统一保留首行调用。全程无 raise。
        if fail := await self.enforce_access(context):
            return fail

        scope_key = context.get("scope_key")
        if not scope_key or not isinstance(scope_key, str):
            # 由 ToolWorker 注入。理论上 system / group:N / private:N 总有一个。
            return ToolOutcome.failure(
                "invalid_arguments",
                "search_history requires scope_key from caller context",
            )
        # session_factory 同样由 ToolWorker 在 context 里注入。ToolWorker 串行
        # 执行工具且全局共用同一个 factory，落到 self 上供 _query /
        # _resolve_task_anchor 复用是安全的（不存在并发 run 互相覆盖）。
        self._session_factory = context.get("session_factory")

        try:
            scope, group_id, _user_id = parse_scope_key(scope_key)
        except ValueError as exc:
            return ToolOutcome.failure(
                "invalid_arguments", f"invalid scope_key {scope_key!r}: {exc}"
            )

        warnings: list[str] = []

        anchor_event_id = _coerce_str(arguments.get("anchor_event_id"))
        task_id = _coerce_str(arguments.get("task_id"))
        start_time = _coerce_str(arguments.get("start_time"))
        end_time = _coerce_str(arguments.get("end_time"))
        query = _coerce_str(arguments.get("query"))
        raw_limit = arguments.get("limit")
        try:
            limit = int(raw_limit) if raw_limit is not None else _DEFAULT_LIMIT
        except (TypeError, ValueError):
            limit = _DEFAULT_LIMIT
        limit = max(1, min(limit, _MAX_LIMIT))

        # task_id → triggered_by_event_id 锚点解析（anchor_event_id 已显式给则优先）
        if not anchor_event_id and task_id:
            anchor_event_id = await self._resolve_task_anchor(task_id)
            if not anchor_event_id:
                warnings.append(
                    f"task_id {task_id!r} has no triggered_by_event_id; "
                    "anchor filter skipped"
                )

        start_dt = _parse_time(start_time) if start_time else None
        end_dt = _parse_time(end_time) if end_time else None
        if start_time and start_dt is None:
            warnings.append(f"start_time {start_time!r} unparseable; ignored")
        if end_time and end_dt is None:
            warnings.append(f"end_time {end_time!r} unparseable; ignored")

        rows = await self._query(
            scope=scope,
            group_id=group_id,
            anchor_event_id=anchor_event_id,
            start_dt=start_dt,
            end_dt=end_dt,
            query=query,
            limit=limit,
        )

        snapshots = [_snapshot_from_row(r) for r in rows]
        # 复用 Projector 的折叠逻辑构造 tool_views，让 tool_call 渲染能拼上结果
        tool_views = Projector.fold_tool_results(snapshots)
        items = Projector.build_timeline(snapshots, tool_views=tool_views)

        return ToolOutcome.success(
            {
                "matched": len(items),
                "anchor_event_id": anchor_event_id,
                "items": [
                    {
                        "event_id": it.event_id,
                        "occurred_at": it.occurred_at.isoformat(),
                        "kind": it.kind,
                        "render": it.render,
                    }
                    for it in items
                ],
                "warnings": warnings,
            }
        )

    async def _resolve_task_anchor(self, task_id: str) -> str | None:
        """查 agent.task_created 事件的 payload.triggered_by_event_id。

        task_created 的 payload 结构由 AgentLoop._apply_actions 写入；
        缺字段 / 找不到事件返回 None，让上层加 warning 而不是炸。
        """
        stmt = (
            select(AgentEvent)
            .where(AgentEvent.type == "agent.task_created")
            .where(AgentEvent.payload["task_id"].astext == task_id)
            .limit(1)
        )
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            row = result.scalars().first()
        if row is None:
            return None
        anchor = (row.payload or {}).get("triggered_by_event_id")
        return str(anchor) if anchor else None

    async def _query(
        self,
        *,
        scope: str,
        group_id: int | None,
        anchor_event_id: str | None,
        start_dt: datetime | None,
        end_dt: datetime | None,
        query: str | None,
        limit: int,
    ) -> list[AgentEvent]:
        stmt = (
            select(AgentEvent)
            .where(AgentEvent.scope == scope)
            .where(AgentEvent.visibility == "agent_visible")
        )
        if scope == "group" and group_id is not None:
            stmt = stmt.where(AgentEvent.group_id == group_id)
        if anchor_event_id:
            stmt = stmt.where(AgentEvent.event_id < anchor_event_id)
        if start_dt is not None:
            stmt = stmt.where(AgentEvent.occurred_at >= start_dt)
        if end_dt is not None:
            stmt = stmt.where(AgentEvent.occurred_at <= end_dt)
        if query:
            # MVP: 直接 ILIKE 子串匹配 payload->>'raw_message'。步骤 6 上 GIN
            # trgm 索引后这条查询会自动走索引，无需改 SQL。
            pattern = f"%{query}%"
            stmt = stmt.where(AgentEvent.payload["raw_message"].astext.ilike(pattern))
        stmt = stmt.order_by(AgentEvent.occurred_at.desc()).limit(limit)

        async with self._session_factory() as session:
            result = await session.execute(stmt)
            rows = result.scalars().all()
        # 投影按时间正序，与正向 timeline 一致
        return list(reversed(rows))


def _coerce_str(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _parse_time(s: str) -> datetime | None:
    """ISO8601 → tz-aware datetime（统一到 Asia/Shanghai）。失败返回 None。"""
    try:
        dt = datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None
    return normalize_china_time(dt)

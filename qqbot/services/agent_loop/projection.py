"""Projector — build DecisionContext from the agent_events stream.

Contract: 开发文档/v2.0/任务与决策契约.md §2.3, §4.2, §5.1, §5.2

Strategy:
- Fetch the newest agent_visible events for this scope (count-limited window).
- Fold `agent.task_*` into TaskView snapshots (active_tasks).
- Pair `agent.tool_called` with `agent.tool_result | agent.tool_failed`
  into ToolResultView。工具视图只有两态：processing（还没 terminal）/
  complete（已 terminal；error_kind 区分成败）。视图**只**用于渲染 timeline
  的 <tool-call> 行——2026-07-02 起不再有独立的 pending_tool_results 区，
  工具结果在 timeline 单点呈现（旧的双重渲染是复读诱饵）。
- Build the timeline from messages / notices / tool-call pairs / replies /
  agent-visible runtime hints. Task and tool-result events are folded
  upstream and do NOT produce timeline rows of their own.

Folding and rendering are split into pure staticmethods so unit tests
can drive them without a DB.

Renderers emit a compact XML envelope. Each renderer:
- Properly escapes user-supplied content (`<`, `>`, `&`, `"`) so chat
  messages cannot inject pseudo-tags into the LLM context.
- Rows carry **no timestamp of their own**（时间流契约 2026-07-26）：
  信封层用 ``render_timeline_stream`` 把相邻同秒的行嵌进同一个
  ``<time when="…">`` 时刻节点——时间是最外层结构，模型对每个事件的第一
  感知是"何时"，然后才是"谁/什么"。``when=`` 为完整 ISO-8601 带时区
  （跨天事件可分辨）。
- Walks OneBot V11 segments structurally (at / reply / image / face /
  poke / record / video / share / forward / ...) instead of dumping the
  raw CQ-code string — see `_render_segments` for the per-type contract.
- Serializes dict/list values as JSON (not Python repr).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
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


def _recap_boundary(recap: _EventSnapshot) -> tuple[datetime, str]:
    """recap 的覆盖边界 (occurred_at, event_id)。载荷缺字段/损坏时退化以
    recap 行自身为界——其之前的事件本就在窗口之外（记忆系统契约 §3.1）。"""
    payload = recap.payload or {}
    boundary_id = payload.get("covers_until_event_id")
    raw_at = payload.get("covers_until_occurred_at")
    boundary_at: datetime | None = None
    if isinstance(raw_at, str):
        try:
            boundary_at = datetime.fromisoformat(raw_at)
        except ValueError:
            boundary_at = None
    if not isinstance(boundary_id, str) or boundary_at is None:
        return recap.occurred_at, recap.event_id
    return boundary_at, boundary_id


class Projector:
    # 单条 tool_result 渲染上限：超过即截断尾部并加 <truncated/>。websearch
    # 等工具的 results 列表很容易爆掉 prompt token，必须兜底。
    # 2026-07-02 从 2048 上调：timeline 的 <tool-call> 行现在是工具结果的
    # **唯一**出口（pending-tool-results 区已删除——它曾是不截断的全量渲染，
    # 模型看长结果全靠它），不上调会让长 websearch 结果的可见部分缩水。
    MAX_TOOL_RESULT_CHARS = 6144

    # 单 task 折叠时保留的最近进度笔记条数。LLM 关心的是最近"我自己想到啥"，
    # 老笔记换 token 不划算。
    MAX_PROGRESS_NOTES_PER_TASK = 5

    # ─── 思考轨迹内联（2026-07-06，待办清单#4）───
    # timeline 渲染的 <my-thought> 行数上限与单条截断长度。每拍（含 idle）
    # 都写 agent.decision_emitted，全量渲染会淹掉真实对话；只保留最近 K 条、
    # 单条截断，且不挤占消息行预算（见 project() 的裁剪逻辑）。
    MAX_THOUGHT_ROWS = 10
    MAX_THOUGHT_CHARS = 300

    # ─── 窗口锚定滞回（2026-07-12，前缀缓存契约）───
    # OpenAI 系 API 的自动前缀缓存要求前缀**逐字节一致**。若裁剪恒取"尾部
    # 正好 max 条"，活跃群每来一条消息窗口起点就前移一行，timeline 的缓存
    # 前缀每拍从起点断掉。改为锚定+滞回：起点钉在上一拍的首行（anchor），
    # 窗口放任增长到 max + SLACK 条才一次性前移回 max 条——起点每 SLACK 条
    # 新行才跳一次，其间各拍共享整段 timeline 前缀。<my-thought> 的"最近
    # K 条"选择边界同理滞回（否则每拍新增一条决策，第 K 旧的思考行就从
    # timeline 中段被抹掉，前缀照断）。锚失效（掉出取数窗 / 重启丢内存态）
    # 时退回朴素裁剪并重新锚定，只多一次缓存 miss。
    TIMELINE_TRIM_SLACK = 30
    THOUGHT_ROWS_SLACK = 5

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        max_items: int = 400,
        max_timeline_items: int = 100,
    ) -> None:
        self._session_factory = session_factory
        # 拉取 fetch 上限保持较大，给 fold_tasks / fold_tool_results 喂够事件；
        # 真正塞给 LLM 的 timeline 在 project() 里再裁到 max_timeline_items 条。
        # 2026-07-27 300→400：记忆系统不变式③（≥ 压缩触发阈值 250 × 1.5），
        # tick 侧未覆盖计数在阈值处不被截断、recap 正常时都在取数窗内。
        self._max_items = max_items
        self._max_timeline_items = max_timeline_items
        # scope_key → 上一拍渲染的 timeline 首行 / 首条 <my-thought> 行的
        # event_id（窗口锚定滞回，见类常量注释）。纯内存态：重启即空，首拍
        # 走朴素裁剪重新锚定，代价只是一次缓存 miss，不落库。
        self._timeline_anchors: dict[str, str] = {}
        self._thought_anchors: dict[str, str] = {}
        # 记忆压缩触顶探针（记忆系统契约 §4.2）：build_context 每拍投影后
        # 回调 (scope_key, 最新 recap 之后的事件数)。压缩器在通知入口校验
        # 阈值；启动/空闲期没有另一路扫描触发。未装配时零开销。
        self._uncovered_notifier: Callable[[str, int], None] | None = None

    def set_uncovered_notifier(
        self, notifier: Callable[[str, int], None] | None
    ) -> None:
        """装配/卸下记忆压缩探针（回调需廉价；异常由 build_context 兜住）。"""
        self._uncovered_notifier = notifier

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
        # 取数窗只按条数收敛（2026-07-27 去除 24h 时间回溯）：模型要能看到
        # 任意早的"最近 N 条"，安静 scope 的旧对话不因时间流逝而消失，退场
        # 唯一路径是被更新的事件挤出条数窗。窗口头部因此只在裁剪重锚时移动，
        # 前缀缓存反而更稳（原 cutoff 按小时取整的动机随之消失）。
        # 逻辑下界（记忆系统契约 §3.1）：最新 runtime.context_compacted
        # （含自身）——更早的事件已折叠进它携带的滚动摘要，不再投影。
        recap: _EventSnapshot | None = None
        recap_boundary: tuple[datetime, str] | None = None
        if scope == "group" and group_id is not None:
            recap = await self._fetch_latest_recap(group_id)
        if recap is not None:
            recap_boundary = _recap_boundary(recap)

        # ``now`` 不只是渲染时钟，也是本拍严格的投影快照上界。消息可能在
        # loop 捕获 now 之后、这条 SELECT 真正执行之前入库；若不设上界，
        # 它会混进本拍 context，却又排在 occurred_at=now 的
        # decision_emitted 之后，下一拍看起来像是本拍已经处理过。明确截到
        # now 后，这类消息留给其自己的 wake/tick 消费。
        events = await self._fetch(
            scope,
            group_id,
            now,
            lower_bound=recap_boundary[0] if recap_boundary else None,
        )
        if recap is not None:
            events = Projector.apply_recap_window(events, recap)
        # 记忆压缩触顶探针：报告"最新摘要之后"的事件数（≈未覆盖数，饱和
        # 于 max_items）。阈值由压缩器入口校验；异常绝不允许影响 tick。
        if self._uncovered_notifier is not None:
            try:
                uncovered = len(events) - (1 if recap is not None else 0)
                self._uncovered_notifier(scope_key, uncovered)
            except Exception as exc:
                logger.warning(
                    "[projection] uncovered notifier failed for {}: {}",
                    scope_key,
                    exc,
                )
        # bot_role 单独一次 SQL 查 —— runtime.bot_role_observed 可能远早于
        # 取数窗（比如启动 sweep 跑过一次就再没变，早被后续事件挤出条数
        # LIMIT），不应受取数窗影响。
        bot_role: str | None = None
        if scope == "group" and group_id is not None:
            bot_role = await self._fetch_latest_bot_role(group_id, bot_user_id)
        ctx = self.project(
            events,
            scope_key=scope_key,
            correlation_id=correlation_id,
            tick_seq=tick_seq,
            now=now,
            max_timeline_items=self._max_timeline_items,
            bot_user_id=bot_user_id,
            bot_role=bot_role,
            timeline_anchor=self._timeline_anchors.get(scope_key),
            thought_anchor=self._thought_anchors.get(scope_key),
            pinned_event_id=recap.event_id if recap is not None else None,
        )
        # 记录本拍窗口锚：timeline 首行 = 下一拍的裁剪起点候选；首条
        # <my-thought> 行 = 下一拍思考选择的起点候选。无思考行时保留旧锚
        # （下一拍找不到会自动退朴素选择，无需清理）。钉住的 recap 行不做
        # 锚——它不参与裁剪计数，锚在它身上会让滞回每拍退回朴素裁剪。
        if ctx.timeline:
            for item in ctx.timeline:
                if recap is not None and item.event_id == recap.event_id:
                    continue
                self._timeline_anchors[scope_key] = item.event_id
                break
            for item in ctx.timeline:
                if item.kind == "my_thought":
                    self._thought_anchors[scope_key] = item.event_id
                    break
        # 任务持久化补全：fold_tasks 只看最近 300 条取数窗，未完成任务的
        # task_created 被水群挤出窗口后会从 active_tasks 消失（与"任务跨 tick
        # 持久"契约冲突，是 bug）。这里查 agent_tasks 读模型，把"仍 pending/
        # running 但已不在窗口内"的任务补回来。窗口内折出的任务优先（更新、带
        # 在途 tool_call_ids），表只填补缺口。读模型不可用时整段降级（仍返回
        # 窗口折叠结果），绝不让 tick 因补全失败而崩。
        ctx = await self._augment_with_persisted_tasks(ctx, scope_key)
        # 表情包收藏夹注入：查 agent_memes 挂到 ctx.saved_memes，llm_planner
        # 渲染成 <saved-memes>（meme 工具凭 hash 操作收藏的选图目录）。同样
        # best-effort 降级——查不到收藏夹只影响本 tick 发不了表情包。
        ctx = await self._augment_with_saved_memes(ctx, scope_key)
        # _augment_with_pending_reply 已于 2026-07-24 删除（待办#19）：它每拍
        # 多查一次 reply_task 事件、只为渲染 <pending-reply>，而那一段的每个
        # Planner 所需字段都被 timeline 上的 <tool-call name="reply"> 行覆盖
        # （reply_task_id / revision / flush_at / hard_deadline 在 <result> 里，
        # analysis / hold_seconds 在 <args> 里）。reply 成功行不再折叠之后主从
        # 关系反转，Planner 信封没有独立状态区。ReplyExecutor 不走这条投影来取
        # 当前授权：它从 reply_task 领域事件折叠最新 analysis，避免 terminal 竞态与
        # timeline 裁剪。
        return ctx

    async def _augment_with_persisted_tasks(
        self, ctx: DecisionContext, scope_key: str
    ) -> DecisionContext:
        try:
            from qqbot.services.agent_loop.task_store import load_active_tasks

            persisted = await load_active_tasks(self._session_factory, scope_key)
        except Exception as exc:  # 读模型查询失败 → 降级为纯窗口折叠
            logger.warning(
                "[projection] load persisted tasks failed for {}: {}",
                scope_key,
                exc,
            )
            return ctx
        merged = Projector.merge_active_tasks(ctx.active_tasks, persisted)
        if merged is ctx.active_tasks:
            return ctx
        from dataclasses import replace

        return replace(ctx, active_tasks=merged)

    async def _augment_with_saved_memes(
        self, ctx: DecisionContext, scope_key: str
    ) -> DecisionContext:
        """收藏夹注入：全局 agent_memes → ctx.saved_memes。

        收藏夹全 bot 一份、所有聊天 scope 共用（隔离契约 §9.2 第 6 条例外，
        见 meme_store 模块 docstring），查询不带 scope 过滤；scope_key 只用来
        判断"有没有聊天面"——system scope 没有（meme 工具的
        allowed_scopes 也不含它），跳过查询省一次 SQL。查询失败整段降级
        （本 tick 不渲染 <saved-memes>，模型只是暂时"想不起收藏"），绝不让
        tick 崩。
        """
        if not scope_key.startswith(("group:", "private:")):
            return ctx
        try:
            from qqbot.services.agent_loop.meme_store import load_saved_memes

            memes = await load_saved_memes(self._session_factory)
        except Exception as exc:  # 读表失败 → 降级为无收藏夹
            logger.warning(
                "[projection] load saved memes failed for {}: {}",
                scope_key,
                exc,
            )
            return ctx
        if not memes:
            return ctx
        from dataclasses import replace

        return replace(ctx, saved_memes=memes)

    @staticmethod
    def merge_active_tasks(
        window_tasks: Sequence[TaskView],
        persisted_tasks: Sequence[TaskView],
    ) -> list[TaskView]:
        """窗口折叠任务 ∪ 读模型任务，按 created_at 升序。

        纯函数（无 DB），便于单测。窗口版本优先：同一 task_id 两边都有时保留
        窗口版（它带在途 tool_call_ids、进度更新；读模型版 pending_tool_call_ids
        恒为空）。读模型只补"窗口里没有"的未完成任务。无补充时原样返回入参
        list（调用方据此判断是否需要 replace）。
        """
        window_ids = {t.task_id for t in window_tasks}
        extra = [t for t in persisted_tasks if t.task_id not in window_ids]
        if not extra:
            # 原样返回入参对象 —— 调用方用 `is` 判断"无变化"省一次 replace。
            return window_tasks  # type: ignore[return-value]
        return sorted([*window_tasks, *extra], key=lambda t: t.created_at)

    async def _fetch_latest_bot_role(
        self,
        group_id: int,
        bot_user_id: str | None,
    ) -> str | None:
        """查该群最新一条 runtime.bot_role_observed。

        不走 _fetch 的条数取数窗与 agent_visible 过滤：
        - 窗口：bot 角色可能很久不变，sweep 后几个月才有 group_admin 事件触发
          下一次写入，这条老事件早被后续事件挤出 LIMIT，硬依赖取数窗会让
          bot_role 凭空消失。
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

    async def _fetch_latest_recap(
        self, group_id: int
    ) -> _EventSnapshot | None:
        """查该群最新一条 runtime.context_compacted（滚动记忆载体）。

        与 bot_role 同理走独立查询、不受取数 LIMIT 约束：积压超过
        max_items 时（压缩器暂时追不上），记忆也不能凭空消失
        （记忆系统契约 §3.2 保底）。"""
        from sqlalchemy import desc

        stmt = (
            select(AgentEvent)
            .where(AgentEvent.type == "runtime.context_compacted")
            .where(AgentEvent.scope == "group")
            .where(AgentEvent.group_id == group_id)
            .order_by(desc(AgentEvent.occurred_at), desc(AgentEvent.event_id))
            .limit(1)
        )
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            rows = list(result.scalars().all())
        return _snapshot_from_row(rows[0]) if rows else None

    async def _fetch(
        self,
        scope: str,
        group_id: int | None,
        upper_bound: datetime,
        lower_bound: datetime | None = None,
    ) -> list[_EventSnapshot]:
        # 无时间回溯下界（2026-07-27 去除 24h 回溯）：窗口只按 LIMIT 收敛，
        # (scope, group_id, occurred_at) 索引倒序扫，表再大也只摸最新
        # max_items 行。lower_bound 是唯一的逻辑下界：最新 recap 的覆盖
        # 边界（粗滤——同时刻残留由 apply_recap_window 按 event_id 精滤）。
        stmt = (
            select(AgentEvent)
            .where(AgentEvent.scope == scope)
            .where(AgentEvent.visibility == "agent_visible")
            .where(AgentEvent.occurred_at <= upper_bound)
        )
        if lower_bound is not None:
            stmt = stmt.where(AgentEvent.occurred_at >= lower_bound)
        if scope == "group" and group_id is not None:
            stmt = stmt.where(AgentEvent.group_id == group_id)
        # event_id 是 ULID；同一 occurred_at 下用它做稳定次序，reverse 后仍是
        # 正向时间序，避免时间戳相同时窗口边缘与行位置随机抖动。
        stmt = stmt.order_by(
            AgentEvent.occurred_at.desc(), AgentEvent.event_id.desc()
        ).limit(self._max_items)

        async with self._session_factory() as session:
            result = await session.execute(stmt)
            rows = result.scalars().all()
        # Reverse to chronological order for downstream folding.
        return [_snapshot_from_row(row) for row in reversed(rows)]

    # ─── Pure projection: testable without DB ───

    @staticmethod
    def apply_recap_window(
        events: Sequence[_EventSnapshot], recap: _EventSnapshot
    ) -> list[_EventSnapshot]:
        """窗口下界精滤（记忆系统契约 §3.1/§3.2）：丢弃全序 ≤ 覆盖边界的
        事件（含更老的 recap 代次），recap 自身保底在场——积压超过取数
        LIMIT 时它会缺席取数结果，此时前插（记忆永不消失）。"""
        boundary = _recap_boundary(recap)
        kept = [
            ev for ev in events if (ev.occurred_at, ev.event_id) > boundary
        ]
        if all(ev.event_id != recap.event_id for ev in kept):
            kept.insert(0, recap)
        return kept

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
        timeline_anchor: str | None = None,
        thought_anchor: str | None = None,
        pinned_event_id: str | None = None,
    ) -> DecisionContext:
        active_tasks = Projector.fold_tasks(events, scope_key=scope_key)
        # tool_views 只喂给 timeline 渲染（<tool-call> 行按两态折叠）；不再
        # 另出 pending_tool_results 区——同一调用双重渲染曾是复读的直接诱饵。
        tool_views = Projector.fold_tool_results(events)
        timeline = Projector.build_timeline(
            events,
            tool_views=tool_views,
            unseen_message_ids=Projector.fold_unseen_message_ids(events),
            thought_anchor=thought_anchor,
        )
        # 裁到尾部 max_timeline_items 条 —— fetch 上限给得宽是为了 fold 任务/
        # 工具结果时能看到足够长的事件链，但塞给 LLM 的不必那么多。
        # <my-thought> 行不占消息行预算（待办清单#4"不挤占"约定）：从尾部
        # 数满 max 条**非思考行**为止，区间内的思考行顺带保留——它们本身已被
        # MAX_THOUGHT_ROWS 封顶，不会失控。timeline_anchor（上一拍窗口首行）
        # 有效时起点滞回钉住，见 _trim_timeline。
        if max_timeline_items is not None:
            # recap 行钉住（记忆系统契约 §3.2）：摘出裁剪再前插——既不占
            # max_timeline_items 行预算，也永不被裁掉（借 MaiBot 的
            # count_in_context=False 思路：记忆不得挤占真实对话的窗口配额）。
            pinned: TimelineItem | None = None
            if pinned_event_id is not None:
                pinned = next(
                    (
                        item
                        for item in timeline
                        if item.event_id == pinned_event_id
                    ),
                    None,
                )
            if pinned is not None:
                rest = [item for item in timeline if item is not pinned]
                rest = Projector._trim_timeline(
                    rest, max_timeline_items, timeline_anchor
                )
                timeline = [pinned, *rest]
            else:
                timeline = Projector._trim_timeline(
                    timeline, max_timeline_items, timeline_anchor
                )
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
            bot_user_id=bot_user_id,
            bot_role=normalized_role,  # type: ignore[arg-type]
        )

    @staticmethod
    def _trim_timeline(
        timeline: list[TimelineItem],
        max_items: int,
        anchor: str | None,
    ) -> list[TimelineItem]:
        """尾部裁剪 + 窗口锚定滞回（前缀缓存契约，见类常量注释）。

        朴素裁剪 = 从尾部数满 ``max_items`` 条非思考行。给了 ``anchor``
        （上一拍窗口首行的 event_id）且它仍在窗内、锚起的非思考行数未超
        ``max_items + TIMELINE_TRIM_SLACK`` 时，起点钉在锚上不动——各拍共享
        同一窗口起点，timeline 前缀逐字节稳定；超出滞回带或锚已失效（掉出
        取数窗 / 重启）则退回朴素裁剪，由 caller 重新锚定。
        """
        naive = -1
        non_thought = 0
        for i in range(len(timeline) - 1, -1, -1):
            if timeline[i].kind != "my_thought":
                non_thought += 1
                if non_thought >= max_items:
                    naive = i
                    break
        if naive <= 0:
            return timeline  # 不足预算（或起点已是首行），整段保留
        if anchor:
            # 锚只可能在朴素起点或更早（窗口只会向前追加）；更新的"锚"说明
            # 状态异常（如配置变更），忽略之走朴素裁剪。
            for idx in range(naive + 1):
                if timeline[idx].event_id != anchor:
                    continue
                kept_non_thought = sum(
                    1 for it in timeline[idx:] if it.kind != "my_thought"
                )
                if (
                    kept_non_thought
                    <= max_items + Projector.TIMELINE_TRIM_SLACK
                ):
                    return timeline[idx:]
                break  # 超出滞回带：一次性前移回朴素起点
        return timeline[naive:]

    @staticmethod
    def fold_unseen_message_ids(
        events: Sequence[_EventSnapshot],
    ) -> frozenset[str]:
        """第一拍判定（2026-07-06，待办清单#1 群聊拆句观望；2026-07-24 删除、
        2026-07-28 复活，见下方"复活说明"）：找出"还没有任何一拍决策看过"
        的新外部消息。

        每条消息入库即 wake(scope)，第一拍常在对方话说到一半时开拍。这里以
        窗口内**最后一条** agent.decision_emitted 为水位线：其后到达的
        external.message.* 即"未见过"，渲染时标 `unseen="true"`（见
        _render_message），把"这拍是这些消息的第一拍"变成结构性事实——
        不靠模型比对 <my-thought time=> 自行推断。窗口内从没有过决策时
        全部消息算未见过（该 scope 真正意义上的第一拍）。

        planner 抛异常的残拍不写 decision_emitted（loop._tick 直接收尾），
        不推进水位线——那一拍确实没"看到"消息，语义自洽。bot 自己的发言
        不会误触发：reply 走 agent.tool_called，不产生 external.message
        事件。notice / request / runtime hint 亦不参与——这个标签只对
        "人还在说话"这件事成立。

        复活说明（2026-07-28）：2026-07-24 删除时的两条理由——① 锚定，模型
        只在带标签的消息里找发言理由；② 一次性，一条消息只享有一拍的
        "值得看"，那拍若观望就永久降级成历史——都没有被推翻，这次是权衡后
        认为"能不能一眼看出哪几行是新的"更重要。同时确认过一个新增代价：
        `unseen="true"` 逐拍变化，消息从 unseen 变为非 unseen 时该行字节
        改变，会在窗口锚定滞回（见类常量注释）之外额外叠加一个前缀缓存
        失效点，且集中在消息密集的场景——用同一个 group 的真实 prompt
        快照估过量级，大致是把"正常拍"的缓存命中率从 65-68% 拉到接近
        "窗口锚点跳动那几拍"的 38-43%。这个取舍是知情做出的，不要当成
        没考虑过的疏漏又删一遍。
        """
        unseen: list[str] = []
        for ev in events:
            if ev.type == "agent.decision_emitted":
                unseen.clear()
            elif ev.type.startswith("external.message."):
                unseen.append(ev.event_id)
        return frozenset(unseen)

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
        """All tool calls in window, paired with their result/failure if any.

        两态折叠：tool_called → processing；terminal（tool_result /
        tool_failed）→ complete。成败不再是独立状态，靠 error_kind 区分
        （None=成功；tool_failed 缺 error_kind 时兜底 "unknown"，保证
        "failed ⇒ error_kind 非 None" 的渲染判据成立）。
        """
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
                    "status": "processing",
                    "result": None,
                    "error_kind": None,
                    "error_message": None,
                    "error_extra": None,
                }
            elif ev.type == "agent.tool_result":
                tc_id = ev.payload.get("tool_call_id")
                if tc_id in calls:
                    calls[tc_id]["status"] = "complete"
                    calls[tc_id]["result"] = ev.payload.get("result")
            elif ev.type == "agent.tool_failed":
                tc_id = ev.payload.get("tool_call_id")
                if tc_id in calls:
                    calls[tc_id]["status"] = "complete"
                    calls[tc_id]["error_kind"] = (
                        ev.payload.get("error_kind") or "unknown"
                    )
                    calls[tc_id]["error_message"] = ev.payload.get("error_message")
                    calls[tc_id]["error_extra"] = _extract_error_extra(ev.payload)
        return [ToolResultView(**d) for d in calls.values()]

    @staticmethod
    def build_timeline(
        events: Sequence[_EventSnapshot],
        *,
        tool_views: Sequence[ToolResultView],
        unseen_message_ids: frozenset[str] | set[str] = frozenset(),
        thought_anchor: str | None = None,
    ) -> list[TimelineItem]:
        tool_view_by_id = {tv.tool_call_id: tv for tv in tool_views}
        # 预扫一遍构建 reply 段引用所需的索引（被回复消息摘要 + 用户名映射），
        # 让单条消息渲染时无需再遍历全部事件。
        excerpt_by_msg_id = _build_excerpt_index(events)
        name_by_user_id = _build_user_name_index(events)
        # author_by_msg_id：被回复消息的作者（_AuthorRef）。reply 段渲染时据此
        # 标 from_name/from_qq/from_self 三个独立属性，让 LLM 一眼看清"是 B 在
        # 引用某人"而不是"某人在发言"——这是 addressee 误判（把别人引用你当成
        # 你说话）的根因修复。覆盖外部消息 + bot 自己已投递的发言（后者标
        # from_self="true"，无需比对 bot_qq 即知"别人引用的是你自己"）。
        author_by_msg_id = _build_author_index(events)
        # 过期完成事件的渲染守卫（重构提案-删除Replyer.md §1.5）：写入侧已在
        # scope 锁内复核，这里是投影侧的第二道防线——更低 revision 的
        # completed、以及 cancelled 任务上迟到的 completed 不渲染。
        reply_task_guard = _build_reply_task_guard(events)

        # ─── 思考轨迹内联（2026-07-06，待办清单#4）───
        # 预扫出要渲染成 <my-thought> 行的决策事件：只保留最近
        # MAX_THOUGHT_ROWS 条、reasoning 非空白的（空白 reasoning 没有内容
        # 可看，跳过）。更早的决策不渲染：思考是辅助记忆，K 之外的旧念头换
        # token 不划算。这些行同时也是"我处理到哪儿"的信号——decision_emitted
        # 的 occurred_at 回填为本拍投影时刻后，行在消息之前=那拍没看到它，
        # 之后=看过了；2026-07-28 起 <message> 自己的 unseen="true"（见
        # fold_unseen_message_ids）是同一件事更直接的信号，行位置规则仍然
        # 成立，当兜底。
        # thought_anchor（上一拍首条思考行）有效时选择起点滞回钉住——否则
        # 每拍新增一条决策就把第 K 旧的思考行从 timeline 中段抹掉，掐断
        # 前缀缓存（见类常量注释）；攒满 K + THOUGHT_ROWS_SLACK 条才一次性
        # 收回最近 K 条。
        thoughts = [
            ev
            for ev in events
            if ev.type == "agent.decision_emitted"
            and isinstance((ev.payload or {}).get("reasoning"), str)
            and str((ev.payload or {}).get("reasoning")).strip()
        ]
        selected = thoughts[-Projector.MAX_THOUGHT_ROWS :]
        if thought_anchor:
            for j, ev in enumerate(thoughts):
                if ev.event_id != thought_anchor:
                    continue
                anchored = thoughts[j:]
                if (
                    len(thoughts) - j
                    <= Projector.MAX_THOUGHT_ROWS
                    + Projector.THOUGHT_ROWS_SLACK
                ):
                    selected = anchored
                break  # 超出滞回带：一次性收回最近 K 条
        thought_ids = {ev.event_id for ev in selected}

        items: list[TimelineItem] = []
        for ev in events:
            if ev.type == "agent.task_state_changed":
                # 任务收束（done/failed）渲染为 <task-closed> 行——模型对
                # "自己刚完成/放弃了什么、结论是什么"的事后记忆（2026-07-02，
                # 此前任务一收束就从 active_tasks 消失、result_summary 蒸发）。
                # 中间态迁移（pending→running 等）仍消隐。
                to_state = (ev.payload or {}).get("to_state")
                if to_state in ("done", "failed"):
                    items.append(
                        TimelineItem(
                            event_id=ev.event_id,
                            occurred_at=ev.occurred_at,
                            kind="task_closed",
                            render=Projector._render_task_closed(ev, to_state),
                        )
                    )
                continue
            if ev.type.startswith("agent.task_"):
                # folded into active_tasks
                continue
            if ev.type in ("agent.tool_result", "agent.tool_failed"):
                # rendered alongside the matching tool_called row。
                # send_messages 也不例外（2026-07-31 实施后调整，维护者拍板）：
                # 调用行的 <args> + <result> 逐条回执就是发言记录，不派生
                # 第二行 <my-reply>——同一句话两处渲染是复读诱饵。终态
                # receipts 仍被 _build_author_index 消费（别人引用 bot 时标
                # from_self）。
                continue
            if ev.type in (
                "agent.reply_task_upserted",
                "agent.reply_task_cancelled",
            ):
                # 领域事件消隐：同一次授权已由它的 <tool-call name="reply"> 行
                # 完整呈现（<args> 授权原文 + <result> 调度事实），再渲染一遍
                # 就是双重渲染。它们仍是 reply_task 折叠的数据源，只是不进
                # timeline。
                continue
            if ev.type == "agent.decision_emitted":
                # 思考轨迹内联（待办清单#4）：最近 K 条渲染 <my-thought> 行
                # ——模型跨拍看得到自己一路在想什么，链条强度不再取决于
                # 单拍深的 <last-reasoning>（该独立区块已随本行删除，防最新
                # 一条双重渲染）。K 之外 / 空白 reasoning 的决策仍消隐。
                if ev.event_id in thought_ids:
                    items.append(
                        TimelineItem(
                            event_id=ev.event_id,
                            occurred_at=ev.occurred_at,
                            kind="my_thought",
                            render=Projector._render_my_thought(ev),
                        )
                    )
                continue
            if ev.type in (
                "agent.reply_emitted",
                "agent.reply_delivered",
                "agent.reply_failed",
                "agent.idle_decision",
            ):
                # reply_emitted/delivered/failed 是历史链路；现役发送事实为
                # runtime.reply_flushed。这里继续跳过旧库遗留事件。idle_decision
                # 是纯运营事件（当拍
                # 的 reasoning 已在 decision_emitted 上渲染 <my-thought>），仍消隐。
                continue

            if ev.type == "agent.tool_called":
                tc_id = ev.payload.get("tool_call_id")
                tv = tool_view_by_id.get(tc_id)
                # 2026-07-24（待办#19）起 reply 的成功行**不再折叠**：它是
                # Planner 回看每次解析的时间线记录——<args> 是该次原文
                # （analysis/hold_seconds），<result> 是它落成的调度事实
                # （reply_task_id/revision/flush_at/hard_deadline）。折叠是为
                # 了避免与 <pending-reply> 双重渲染，而那一段已随本次改动删除，
                # Planner 内容留在 timeline，不另立状态区。到点后的当前
                # analysis 由 <reply-task-completed> 行自包含送达，不依赖这行
                # 是否 terminal/仍在窗口。
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
                    ev,
                    excerpt_by_msg_id,
                    name_by_user_id,
                    author_by_msg_id,
                    unseen=ev.event_id in unseen_message_ids,
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
                        render=Projector._render_notice(ev, name_by_user_id),
                    )
                )
            elif ev.type.startswith("external.request."):
                items.append(
                    TimelineItem(
                        event_id=ev.event_id,
                        occurred_at=ev.occurred_at,
                        kind="request",
                        render=Projector._render_request(ev),
                    )
                )
            elif ev.type == "runtime.reply_flushed":
                # 旧链路历史事件（2026-07-31 起新链路不写 flushed；现役发言
                # 记录是 send_messages 的 <tool-call> 行）。
                items.append(
                    TimelineItem(
                        event_id=ev.event_id,
                        occurred_at=ev.occurred_at,
                        kind="my_reply",
                        render=Projector._render_reply_flushed(ev),
                    )
                )
            elif ev.type == "runtime.reply_task_completed":
                if _completed_is_stale(ev, reply_task_guard):
                    continue
                items.append(
                    TimelineItem(
                        event_id=ev.event_id,
                        occurred_at=ev.occurred_at,
                        kind="reply_task_completed",
                        render=Projector._render_reply_task_completed(ev),
                    )
                )
            elif ev.type == "runtime.context_compacted":
                # 滚动记忆摘要行（记忆系统契约 §3.3）：专门渲染，不落
                # runtime JSON 兜底——那会把 recall_cues 等内部字段一起
                # 倾倒进 prompt。
                items.append(
                    TimelineItem(
                        event_id=ev.event_id,
                        occurred_at=ev.occurred_at,
                        kind="system_hint",
                        render=Projector._render_context_recap(ev),
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
        author_by_msg_id: "dict[str, _AuthorRef] | None" = None,
        *,
        unseen: bool = False,
    ) -> tuple[str, list[ImageRef]]:
        sender = ev.payload.get("sender") or {}
        name = sender.get("card") or sender.get("nickname")
        qq = sender.get("user_id") or ev.user_id
        # 匿名群消息（OneBot 标准字段；napcat 不支持匿名、恒缺失）：发送者
        # 顶着匿名马甲，sender_name 退到匿名昵称，并标 anonymous="true" 让
        # LLM 知道这名字不是真实群成员身份。anonymous.flag 是禁言凭证，
        # 只入库不渲染（凭证不经 LLM，与 request.flag 同策略）。
        anonymous = ev.payload.get("anonymous")
        if not isinstance(anonymous, dict):
            anonymous = None
        if not name and anonymous:
            anon_name = anonymous.get("name")
            if anon_name:
                name = str(anon_name)
        msg_id = ev.payload.get("onebot_message_id") or ""

        segments = ev.payload.get("segments") or []
        body, images = _render_segments(
            segments, excerpt_by_msg_id, name_by_user_id, author_by_msg_id
        )
        # raw_message 兜底：mapper 上游异常时 segments 可能为空但 raw_message 还在
        if not body:
            raw = ev.payload.get("raw_message", "")
            if raw:
                body = _esc_text(str(raw))

        # 行内不带时间（时间流契约，见模块 docstring）：时刻由外层 <time>
        # 节点承载。sender_name / sender_qq 是两个独立属性——不再拼 "昵称(QQ)"
        # 复合串，模型无需拆括号即可拿到 @人 / 工具 user_id 参数要用的号。
        # _qq 后缀显式标注 ID 空间（QQ 号），与 message_id / event_id 一眼
        # 可分。缺哪个省哪个（=未知），不造 "?" 占位。
        attrs = []
        if name:
            attrs.append(f'sender_name="{_esc_attr(str(name))}"')
        if qq is not None:
            attrs.append(f'sender_qq="{_esc_attr(str(qq))}"')
        # sender_role：发送者在本群的角色。只在 owner/admin 时渲染——napcat 的
        # sender.role 三值 owner/admin/member，member 是绝大多数，逐条渲染纯耗
        # token；缺省语义（普通成员或未知）在 envelope.md 里写死，无歧义。
        role = str(sender.get("role") or "").strip().lower()
        if role in ("owner", "admin"):
            attrs.append(f'sender_role="{role}"')
        # sender_title：群专属头衔（napcat 消息事件不上报，其他 OneBot 实现
        # 可能给）。有才渲染，社交语境线索。
        title = str(sender.get("title") or "").strip()
        if title:
            attrs.append(f'sender_title="{_esc_attr(title)}"')
        if anonymous:
            attrs.append('anonymous="true"')
        # message_id= 而非裸 id=：显式标注这是 OneBot 消息 ID 空间（引用 /
        # recall / set_essence 等工具参数同名直抄），与 task_id / event_id 区分。
        if msg_id:
            attrs.append(f'message_id="{_esc_attr(str(msg_id))}"')
        # unseen="true"：该消息在最后一条 agent.decision_emitted 之后到达，
        # 还没有任何一拍处理过（fold_unseen_message_ids）。缺失 = 已经历过
        # 至少一拍。属性总语义"缺失=默认"的又一实例。
        if unseen:
            attrs.append('unseen="true"')
        attr_str = f" {' '.join(attrs)}" if attrs else ""
        return f"<message{attr_str}>{body}</message>", images

    @staticmethod
    def _render_notice(
        ev: _EventSnapshot,
        name_by_user_id: dict[str, str] | None = None,
    ) -> str:
        """渲染 ``<notice/>``。属性语义（缺失一律表示"未知/不适用"）：

        - ``user_qq``   事件的当事人 QQ（谁入群/被禁言/被戳/名片被改/消息被回应）
        - ``operator_qq`` 执行动作的人 QQ（禁言的管理员、撤回的人）
        - ``target_qq`` 动作的承受方 QQ（poke 里被戳的人）
        - ``user_name`` / ``operator_name`` / ``target_name``：上述 QQ 在近期
          消息里出现过时反查到的名字——mapper 只存了裸 QQ 号，不给名字的话
          LLM 得自己翻 timeline 对号入座，经常对错。
        - kind 专属明细（duration_seconds / old_card / new_card / file_name /
          file_size_bytes / message_id / likes / honor_type）见
          ``_notice_detail_attrs``。这些字段 mapper 早已入库，历史上渲染时全部
          丢弃，是"事件发生了但 LLM 不知道内容"的主要来源。
        """
        names = name_by_user_id or {}
        kind = ev.type.replace("external.notice.", "")
        attrs = [f'kind="{_esc_attr(kind)}"']
        sub = ev.payload.get("sub_type")
        if sub:
            attrs.append(f'sub_type="{_esc_attr(str(sub))}"')
        if ev.user_id is not None:
            attrs.append(f'user_qq="{ev.user_id}"')
            _append_name_attr(attrs, "user_name", ev.user_id, names)
        op = ev.payload.get("operator_id")
        if op:
            attrs.append(f'operator_qq="{op}"')
            _append_name_attr(attrs, "operator_name", op, names)
        target = ev.payload.get("target_id")
        if target:
            attrs.append(f'target_qq="{target}"')
            _append_name_attr(attrs, "target_name", target, names)
        attrs.extend(_notice_detail_attrs(kind, ev.payload))
        return f"<notice {' '.join(attrs)}/>"

    @staticmethod
    def _render_request(ev: _EventSnapshot) -> str:
        """渲染 external.request.*。

        2026-07-03 拆分后实际会渲染的只有入群申请（``external.request.group.add``，
        scope=group 进目标群 timeline）：群内 LLM 看到后可提醒，管理员明确授权后
        调 respond_to_group_join_request 回执 napcat（事件系统设计.md §10.2）。
        好友申请 / 邀请入群是 runtime_only（自动审批），永远不会走到这里；渲染
        逻辑仍按 type 前缀泛化，不对 kind 特判。

        关键：渲染必须带 event_id —— LLM 调工具时回填它，工具用 event_id 反查
        事件 payload 里的 flag，这样 napcat 的 flag 凭证不经过 LLM 复述，避免
        长串照抄出错。comment 是申请人填的验证留言（提醒/决策的主要依据）。
        """
        kind = ev.type.replace("external.request.", "")
        attrs = [
            f'kind="{_esc_attr(kind)}"',
            f'event_id="{_esc_attr(str(ev.event_id))}"',
        ]
        if ev.user_id is not None:
            attrs.append(f'user_qq="{ev.user_id}"')
        group_id = ev.payload.get("group_id")
        if group_id:
            attrs.append(f'group_id="{group_id}"')
        comment = ev.payload.get("comment")
        if comment:
            attrs.append(f'comment="{_esc_attr(str(comment))}"')
        return f"<request {' '.join(attrs)}/>"

    @staticmethod
    def _render_tool_call(
        ev: _EventSnapshot, tv: ToolResultView | None
    ) -> str:
        """两态渲染：processing → <processing/>；complete → <result>（成功，
        error_kind 为 None）或 <error>（失败）。status 属性只回答"结束没有"，
        成败让 LLM 看子元素——与 ToolResultView 的两态语义一致。

        发起时刻由外层 <time when="…"> 流节点承载（render_timeline_stream，
        时间流契约 2026-07-26），行内不带 time= 属性。"""
        name = str(ev.payload.get("tool_name", "?"))
        args = ev.payload.get("arguments", {})
        args_json = _safe_json(args)
        if tv is None or tv.status == "processing":
            inner = "<processing/>"
            status = "processing"
        elif tv.error_kind is None:
            result_json = _safe_json(tv.result)
            truncated = ""
            if len(result_json) > Projector.MAX_TOOL_RESULT_CHARS:
                result_json = result_json[: Projector.MAX_TOOL_RESULT_CHARS]
                truncated = "<truncated/>"
            inner = f"<result>{_esc_text(result_json)}{truncated}</result>"
            status = "complete"
        else:
            inner = _render_error_element(
                tv.error_kind, tv.error_message, tv.error_extra
            )
            status = "complete"
        return (
            f'<tool-call name="{_esc_attr(name)}" status="{status}">'
            f"<args>{_esc_text(args_json)}</args>{inner}</tool-call>"
        )

    @staticmethod
    def _render_task_closed(ev: _EventSnapshot, outcome: str) -> str:
        """agent.task_state_changed(done|failed) → <task-closed> 行。

        正文是模型自己写的 result_summary / 失败原因（task 工具 complete / fail
        分支落进 payload.reason）——收束后 active_tasks 里不再有
        这个任务，本行是"这事我已经办完了、结论是什么"的唯一事后记忆。防御性
        截 600 字（模型摘要正常远短于此）。
        """
        payload = ev.payload or {}
        task_id = str(payload.get("task_id") or "?")
        reason = payload.get("reason")
        body = ""
        if isinstance(reason, str) and reason.strip():
            body = _esc_text(reason.strip()[:600])
        return (
            f'<task-closed task_id="{_esc_attr(task_id)}" '
            f'outcome="{_esc_attr(outcome)}">'
            f"{body}</task-closed>"
        )

    @staticmethod
    def _render_my_thought(ev: _EventSnapshot) -> str:
        """agent.decision_emitted → <my-thought> 行（思考轨迹内联，待办清单#4）。

        正文是模型当拍的 reasoning 原文，截 MAX_THOUGHT_CHARS 字防长独白
        挤占窗口。它是记忆不是指令——配套硬规则（念头≠动作：念头后没有对应
        tool-call 即那件事没发生；旧思考里的草稿措辞不得直接当消息发出）在
        planner.md §这个系统是这样运行的 与 envelope.md §<my-thought>。
        """
        reasoning = str((ev.payload or {}).get("reasoning") or "").strip()
        if len(reasoning) > Projector.MAX_THOUGHT_CHARS:
            reasoning = reasoning[: Projector.MAX_THOUGHT_CHARS] + "…"
        return f"<my-thought>{_esc_text(reasoning)}</my-thought>"

    @staticmethod
    def _render_context_recap(ev: _EventSnapshot) -> str:
        """记忆摘要（runtime.context_compacted）：正文 = 覆盖区间 + 条数 +
        摘要全文 + 从属脚注；不渲染 recall_cues / 内部字段。"""
        payload = ev.payload or {}
        summary = str(payload.get("summary") or "").strip()
        head = "[早前对话回忆]"
        frm = str(payload.get("covers_from_occurred_at") or "")[:16]
        until = str(payload.get("covers_until_occurred_at") or "")[:16]
        if frm and until:
            head += f" 覆盖 {frm} 至 {until}"
        count = payload.get("dropped_event_count")
        if isinstance(count, int):
            head += f" 共 {count} 条"
        body = (
            f"{head}\n{summary}\n"
            "（回忆由更早对话压缩而来，仅供参考；与当前对话冲突时，"
            "以当前对话为准。）"
        )
        return (
            '<system-hint kind="context_compacted">'
            f"{_esc_text(body)}</system-hint>"
        )

    @staticmethod
    def _render_runtime(ev: _EventSnapshot) -> str:
        kind = ev.type.replace("runtime.", "")
        payload = ev.payload or {}
        if ev.type == "runtime.tool_batch_completed":
            # 批次收口标记（agent_visible）：让模型显式看到"上一批工具已
            # 整体到终态"的边界。payload 里的 tool_batch_id 是内部 ULID，
            # 只服务于共享工具批次收口查重，对模型零信息量——渲染时剔除，
            # 只留 tool_count / tool_batch_size。
            payload = {
                k: v
                for k, v in payload.items()
                if k in ("tool_count", "tool_batch_size") and v is not None
            }
        payload_json = _safe_json(payload)
        return (
            f'<system-hint kind="{_esc_attr(kind)}">'
            f"{_esc_text(payload_json)}</system-hint>"
        )

    @staticmethod
    def _render_reply_task_completed(ev: _EventSnapshot) -> str:
        """runtime.reply_task_completed → <reply-task-completed> 行。

        只陈述"这份稿的等待阶段结束了"与最终 analysis 原文；没有授权 ID、
        unseen、consumed 或 expires_at——元素命名刻意不表达任何发言权限
        （重构提案-删除Replyer.md §5.4）。
        """
        payload = ev.payload or {}
        attrs = [
            f'reply_task_id="{_esc_attr(str(payload.get("reply_task_id") or ""))}"',
            f'revision="{_esc_attr(str(payload.get("revision") or ""))}"',
        ]
        analysis = str(payload.get("analysis") or "").strip()
        return (
            f"<reply-task-completed {' '.join(attrs)}>"
            f"<analysis>{_esc_text(analysis)}</analysis>"
            "</reply-task-completed>"
        )

    @staticmethod
    def _render_reply_flushed(ev: _EventSnapshot) -> str:
        """旧链路 runtime.reply_flushed → <my-reply>（仅历史兼容渲染）。

        现役发言不产生本行：一次发送的记录就是它的
        `<tool-call name="send_messages">` 行（args + 结果回执）。
        """
        payload = ev.payload or {}
        attrs = []
        task_id = payload.get("reply_task_id")
        if task_id:
            attrs.append(f'reply_task_id="{_esc_attr(str(task_id))}"')
        attrs.append(
            f'status="{_esc_attr(str(payload.get("status") or "unknown"))}"'
        )
        parts = [f"<my-reply {' '.join(attrs)}>"]
        for item in payload.get("sent_messages") or []:
            if not isinstance(item, dict):
                continue
            status = _esc_attr(str(item.get("status") or "unknown"))
            mid = item.get("message_id")
            mid_attr = (
                f' message_id="{_esc_attr(str(mid))}"' if mid is not None else ""
            )
            if item.get("kind") == "meme":
                image_hash = _esc_attr(str(item.get("image_hash") or ""))
                parts.append(
                    f'<sent-meme status="{status}"{mid_attr} hash="{image_hash}"/>'
                )
            else:
                body, _ = _render_segments(
                    item.get("content") or [], {}, {}, {}
                )
                parts.append(
                    f'<sent-message status="{status}"{mid_attr}>{body}</sent-message>'
                )
        reason = payload.get("reason")
        if reason:
            parts.append(f"<reason>{_esc_text(str(reason))}</reason>")
        parts.append("</my-reply>")
        return "".join(parts)


# ─── 时间流渲染（时间流契约 2026-07-26）───


def render_timeline_stream(items: Sequence[TimelineItem]) -> list[str]:
    """timeline 行 → ``<time when="…">`` 时刻节点序列（信封层唯一入口）。

    时间是最外层结构：``<timeline>`` 的直接子元素是时刻节点，事件行嵌在
    节点内、自身不带任何时间属性——模型对每个事件的第一感知是"何时"。
    相邻且同秒（timespec=seconds，与旧行内 time= 同精度）的行共享同一
    节点：同拍派发的工具批次、同秒消息 burst 自然聚簇为"这一刻发生了
    这些"。属性名取 when= 而非 at=——at 与 <at>（@人）标签撞词是已知的
    模型混淆源。

    信封组装层必须经由本函数渲染 timeline（历史上 Planner 与 Replyer 两个
    组装层靠它保证逐字节同构；2026-07-31 删除 Replyer 后只剩 Planner 一个
    消费者，单一入口的约定保留）。前缀缓存：新秒的行整块追加；同秒
    追加只重写上一个 ``</time>`` 闭合位置——破坏面与旧的逐行追加持平。
    """
    parts: list[str] = []
    open_when: str | None = None
    for item in items:
        when = item.occurred_at.isoformat(timespec="seconds")
        if when != open_when:
            if open_when is not None:
                parts.append("</time>")
            parts.append(f'<time when="{_esc_attr(when)}">')
            open_when = when
        parts.append(item.render)
    if open_when is not None:
        parts.append("</time>")
    return parts


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


# agent.tool_failed.payload 顶层的"信封字段"——不属于结构化失败附加信息（extra）。
# 工具执行层把 payload 拼成 {tool_call_id, tool_name, task_id, error_kind,
# error_message, **outcome.extra}，fold_tool_results 据此把其余键收进
# ToolResultView.error_extra，再由 _render_error_element 透给 LLM。
_TOOL_FAILED_ENVELOPE_KEYS = frozenset(
    {"tool_call_id", "tool_name", "task_id", "error_kind", "error_message"}
)


def _extract_error_extra(payload: dict) -> dict | None:
    """从 tool_failed.payload 顶层收出结构化失败附加字段（ToolOutcome.extra
    平铺进来的那些：required_tier / actual_tier / retcode / action ...），
    剔除信封字段与 None 值。全空则返回 None。"""
    extra = {
        k: v
        for k, v in (payload or {}).items()
        if k not in _TOOL_FAILED_ENVELOPE_KEYS and v is not None
    }
    return extra or None


def _is_safe_attr_key(key: object) -> bool:
    """extra 键能否安全当 XML 属性名：仅允许 ASCII 字母/数字/下划线且非数字开头。

    extra 键全是代码字面量（required_tier / retcode / ...），本无注入风险；这里
    只是防御未来某个工具塞进奇怪键名破坏 <error> 标签。不合规的键静默跳过。"""
    if not isinstance(key, str) or not key:
        return False
    if not (key[0].isascii() and (key[0].isalpha() or key[0] == "_")):
        return False
    return all(c.isascii() and (c.isalnum() or c == "_") for c in key)


def _render_error_element(
    error_kind: str | None,
    error_message: str | None,
    error_extra: dict | None = None,
) -> str:
    """渲染失败 ``<error>`` 元素：``kind`` + 结构化 ``error_extra`` 全部作为属性
    透出，人类可读原因作正文。timeline ``<tool-call>`` 的失败渲染唯一入口
    （曾与 llm_planner 的 pending-tool-results 区共用；该区已删除）。

    ``error_extra``（required_tier / actual_tier / required_bot_role /
    actual_bot_role / retcode / action / allowed_scopes ...）是工具失败时
    ``ToolOutcome.extra`` 平铺进 ``tool_failed.payload`` 的结构化字段。历史上这里
    只渲染 kind+message，把它们丢了——envelope.md 承诺 payload 带
    required_tier/actual_tier，却从没真到模型。现在逐个作属性透出：标量原样、
    列表/字典 JSON 编码，让 LLM 精确解释"差在哪一级权限 / napcat 具体报了什么"。

    键做标识符白名单过滤（``_is_safe_attr_key``）防属性注入；单值超 200 字截断，
    避免个别工具塞大对象撑爆 prompt。"""
    attrs = [f'kind="{_esc_attr(str(error_kind or ""))}"']
    for key, value in (error_extra or {}).items():
        if value is None or not _is_safe_attr_key(key):
            continue
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, (str, int, float)):
            rendered = str(value)
        else:
            rendered = _safe_json(value)
        if len(rendered) > 200:
            rendered = rendered[:200] + "…"
        attrs.append(f'{key}="{_esc_attr(rendered)}"')
    return (
        f"<error {' '.join(attrs)}>"
        f"{_esc_text(str(error_message or ''))}</error>"
    )


# ─── Segment-level rendering ───


def _render_segments(
    segments: Iterable,
    excerpt_by_msg_id: dict[str, str],
    name_by_user_id: dict[str, str],
    author_by_msg_id: "dict[str, _AuthorRef] | None" = None,
) -> tuple[str, list[ImageRef]]:
    """把 OneBot V11 段数组翻译成内联 XML 标签 + 收集已落盘的 ImageRef。

    支持的段类型 → 标签（属性一律"缺失=未知/不适用"，语义与 envelope.md
    §内联段 一一对应，两处必须同步改）：
      text     → 原文（XML escape）
      at       → <at qq="..." name="..."/> 或 <at-all/>（qq= 与出站段
                 data.qq 同名同值，模型可直抄）
      reply    → <reply to_message_id="..." from_name="昵称" from_qq="QQ"
                        from_self="true" excerpt="..."/>
                 （作者三属性各自独立、缺哪个省哪个：from_name/from_qq 标注
                  被引用消息的作者，author_by_msg_id 命中时；from_self 仅在
                  被引消息是 bot 自己发的时渲染 "true"；excerpt= 在 timeline
                  内可查时）
      image    → <image kind="photo|sticker" summary="外显文案" hash="sha256"
                        desc="VLM 客观描述"/>
                 kind：napcat data.sub_type 0→photo（相册照片/截图类），
                 1→sticker（表情包），或 data.emoji_id 存在→sticker（商城
                 表情——napcat 接收侧 mface 一律折成 image 段到达）；判断
                 不出则不渲染该属性。summary：QQ 的外显文案（如 "[动画表情]"
                 或商城表情名 "[赞]"），下载失败时它是唯一语义兜底。
                 desc：2026-07-28 起 Planner/Replyer 是纯文本模型，这段
                 ingest 期生成的客观转录是它们看到这张图的**唯一**途径。
                 描述随消息内联渲染（就在它所属的那条 <message> 里），图文
                 时序天然对齐，不再需要旧多模态路径的 hash label 对位。
                 未描述成功（未配置 VLM / 调用失败 / 图没下载下来）则不渲染
                 该属性——模型知道有图但看不到内容，可调 look_at_image 补看。
      face     → <face face_id="N" name="[微笑]"/>（QQ 原生黄豆表情；name 取
                 napcat data.raw.faceText，LLM 背不出表情 id 表，没名字
                 的 face id 是纯噪声）
      mface    → <mface summary="[赞]"/>（兼容非 napcat 的 OneBot 实现；
                 napcat 不会在接收侧产生 mface 段，见 image 行）
      record   → <voice/>            (LLM 当前不消费语音内容)
      video    → <video/>            (同上)
      file     → <file name="..." size_bytes="..." file_id="..."/>
                 （size_bytes 单位字节；file_id 是 napcat 文件凭证，供
                  未来的文件下载类工具回填）
      poke     → <poke target_qq="..."/>（napcat 群内拍一拍段不带目标 → <poke/>）
      dice     → <dice value="N"/>   (掷骰子结果 1-6)
      rps      → <rps value="N"/>    (猜拳 1=石头 2=剪刀 3=布)
      markdown → <markdown>正文</markdown>（napcat 给了 data.content，官方
                 机器人消息常见；超 _MAX_MARKDOWN_CHARS 截断加 "…"）
      forward  → <forward forward_id="..."/>
      json     → ark 卡片，走 _render_card_segment 解析出
                 <card app/summary/title/desc/url>；B 站分享、小程序、公众号
                 文章等在 napcat 全部以 json 段到达，解析不出任何字段才回退
                 <card format="json"/>
      share    → <card format="share" title="..." desc="..." url="..."/>
                 （OneBot 标准段；napcat 不产生，兼容保留）
      xml      → <card format="xml"/>（napcat 收发均不产生，兼容保留）
      其他     → <misc segment_type="..."/>

    image segment 的富化字段 (file_hash / local_path / mime / downloaded /
    description) 由 event_ingest/media.py 写在 segment 顶层（不在 data 内），见
    EventIngest契约.md §6.1。downloaded=true 且 local_path 存在的图片才会进
    images 列表 —— 2026-07-28 起没有任何 prompt 装配路径消费它（Planner/Replyer
    已不看像素），保留是因为它仍是"这条消息带了哪些已落盘图片"的结构化记录。
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
                        f'<at qq="{_esc_attr(qq)}" name="{_esc_attr(nm)}"/>'
                    )
                else:
                    parts.append(f'<at qq="{_esc_attr(qq)}"/>')
            else:
                parts.append("<at/>")
        elif t == "reply":
            rid = str(d.get("id", "")).strip()
            if rid:
                attrs = [f'to_message_id="{_esc_attr(rid)}"']
                # from_name/from_qq/from_self：被引用消息的作者，三个独立属性
                # （不再拼 "昵称(QQ)" / "我(id)" 复合串，模型无需拆括号）。这是
                # "别人引用我 ≠ 我在发言"的关键——没有它，LLM 看到被引用内容
                # 内联在画面里，容易当成对方刚说的话。缺哪个省哪个（=未知）。
                #
                # 取值优先级（2026-07-22 出窗引用黑洞修复）：ingest 富化的
                # segment 顶层 quoted 键 > 投影窗口内索引。quoted 在消息到达
                # 时由适配器已解析的 event.reply 固化（EventIngest契约 §6.4），
                # 不随窗口滚动丢失；旧库事件无 quoted，仍靠窗口索引兜底。
                # 逐字段回退：quoted 缺个别子键时该字段仍可由索引补上。
                quoted = seg.get("quoted")
                if not isinstance(quoted, dict):
                    quoted = {}
                author = (author_by_msg_id or {}).get(rid)
                from_name = _nonempty_str(quoted.get("sender_name")) or (
                    author.name if author else None
                )
                from_qq = _nonempty_str(quoted.get("sender_qq")) or (
                    author.user_id if author else None
                )
                # 仅确证是 bot 自己的消息渲染 from_self="true"；外部作者不
                # 渲染 false——"是不是我"的常规判据是 from_qq == bot_qq，
                # from_self 出现即铁证（服务端标注，不依赖 bot_qq）。
                from_self = quoted.get("from_self") is True or (
                    author.is_self if author else False
                )
                if from_name:
                    attrs.append(f'from_name="{_esc_attr(from_name)}"')
                if from_qq:
                    attrs.append(f'from_qq="{_esc_attr(from_qq)}"')
                if from_self:
                    attrs.append('from_self="true"')
                excerpt = _gloss_segments(
                    quoted.get("segments") or []
                ) or excerpt_by_msg_id.get(rid)
                if excerpt:
                    attrs.append(f'excerpt="{_esc_attr(excerpt)}"')
                parts.append(f'<reply {" ".join(attrs)}/>')
            else:
                parts.append("<reply/>")
        elif t == "image":
            attrs = []
            kind = _image_kind(d)
            if kind:
                attrs.append(f'kind="{kind}"')
            summary = str(d.get("summary") or "").strip()
            if summary:
                attrs.append(f'summary="{_esc_attr(_clip(summary, 50))}"')
            # 富化字段写在 segment 顶层（media.py 契约），不在 data 内
            file_hash = seg.get("file_hash")
            if file_hash:
                attrs.append(f'hash="{_esc_attr(str(file_hash))}"')
                # ImageRef 的收集条件只看 downloaded + local_path，与有没有
                # description 无关（描述失败的图仍是"这条消息带了这张图"）。
                if seg.get("downloaded") and seg.get("local_path"):
                    images.append(
                        ImageRef(
                            file_hash=str(file_hash),
                            local_path=str(seg["local_path"]),
                            mime=str(seg.get("mime") or "image/png"),
                        )
                    )
            # desc：ingest 期 VLM 写的客观转录，模型看图的唯一途径。写时已按
            # MAX_DESCRIPTION_CHARS 截断，这里再兜一道上界防历史脏数据把整个
            # timeline 冲垮。空白折成单空格——_esc_attr 不处理换行，多行描述直
            # 接塞进属性会把 XML 信封排版打散。
            description = str(seg.get("description") or "").strip()
            if description:
                flattened = " ".join(description.split())
                attrs.append(
                    f'desc="{_esc_attr(_clip(flattened, _MAX_IMAGE_DESC_CHARS))}"'
                )
            parts.append(f"<image {' '.join(attrs)}/>" if attrs else "<image/>")
        elif t == "face":
            fid = str(d.get("id", "")).strip()
            fname = _face_name(d)
            if fid and fname:
                parts.append(
                    f'<face face_id="{_esc_attr(fid)}" name="{_esc_attr(fname)}"/>'
                )
            elif fid:
                parts.append(f'<face face_id="{_esc_attr(fid)}"/>')
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
            attrs = []
            fname = str(d.get("name", "") or d.get("file", "")).strip()
            if fname:
                attrs.append(f'name="{_esc_attr(fname)}"')
            fsize = d.get("file_size")
            if fsize is not None and str(fsize).strip():
                attrs.append(f'size_bytes="{_esc_attr(str(fsize))}"')
            file_id = d.get("file_id")
            if file_id is not None and str(file_id).strip():
                attrs.append(f'file_id="{_esc_attr(str(file_id))}"')
            parts.append(f"<file {' '.join(attrs)}/>" if attrs else "<file/>")
        elif t == "poke":
            target = d.get("qq") or d.get("user_id")
            if target:
                parts.append(f'<poke target_qq="{_esc_attr(str(target))}"/>')
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
            content = str(d.get("content") or "").strip()
            if content:
                parts.append(
                    f"<markdown>{_esc_text(_clip(content, _MAX_MARKDOWN_CHARS))}"
                    "</markdown>"
                )
            else:
                parts.append("<markdown/>")
        elif t == "forward":
            fid = str(d.get("id", "")).strip()
            if fid:
                parts.append(f'<forward forward_id="{_esc_attr(fid)}"/>')
            else:
                parts.append("<forward/>")
        elif t == "json":
            parts.append(_render_card_segment(d))
        elif t == "share":
            # OneBot 标准 share 段（napcat 不产生，兼容其他实现）。字段名
            # 与 ark 卡片对齐：content → desc。
            attrs = ['format="share"']
            title = str(d.get("title") or "").strip()
            if title:
                attrs.append(f'title="{_esc_attr(_clip(title, 100))}"')
            desc = str(d.get("content") or "").strip()
            if desc:
                attrs.append(f'desc="{_esc_attr(_clip(desc, 200))}"')
            url = str(d.get("url") or "").strip()
            if url:
                attrs.append(f'url="{_esc_attr(_clip(url, 300))}"')
            parts.append(f"<card {' '.join(attrs)}/>")
        elif t == "xml":
            parts.append('<card format="xml"/>')
        else:
            parts.append(
                f'<misc segment_type="{_esc_attr(str(t or "unknown"))}"/>'
            )
    return "".join(parts), images


# markdown 段正文渲染上限：官方机器人可能发整页 md，塞满 prompt 不值。
_MAX_MARKDOWN_CHARS = 500
# 图片客观描述的渲染上界。与 image_description.MAX_DESCRIPTION_CHARS 同值但不
# import —— 那是写入端的截断，这里是渲染端对任意来源（含历史事件）的兜底。
_MAX_IMAGE_DESC_CHARS = 1200


def _clip(s: str, limit: int) -> str:
    """超长截断加 "…"。属性值通用，保证单个字段不会撑爆 prompt。"""
    return s if len(s) <= limit else s[:limit] + "…"


def _image_kind(d: dict) -> str | None:
    """推断图片语义类别，返回 "photo" / "sticker" / None（判断不出）。

    依据（NapCatQQ rawToOb11Converters 实测行为）：
    - data.emoji_id 存在 → 商城表情（napcat 把 marketFace 折成 image 段上报，
      带 emoji_id / emoji_package_id / key）→ sticker；
    - data.sub_type 是 NTQQ PicSubType：0=KNORMAL 普通图片 → photo，
      1=KCUSTOM 自定义表情/表情包 → sticker；
    - 其余取值（2=KHOT 等罕见类型）与缺失一律 None——宁可不标也不猜错，
      "缺失=未知"是本渲染器的属性总语义。
    """
    if d.get("emoji_id"):
        return "sticker"
    sub = d.get("sub_type")
    if sub is None:
        return None
    s = str(sub).strip()
    if s == "0":
        return "photo"
    if s == "1":
        return "sticker"
    return None


def _face_name(d: dict) -> str | None:
    """从 napcat face 段的 data.raw.faceText 取表情释义（如 "[微笑]"）。

    faceText 在部分老版本里带 QQ 输入法风格的 "/" 前缀（"/微笑"），去掉；
    raw 缺失 / faceText 为空 → None（渲染方退回只有 id 的形态）。
    """
    raw = d.get("raw")
    if not isinstance(raw, dict):
        return None
    text = raw.get("faceText")
    if not isinstance(text, str):
        return None
    cleaned = text.strip().lstrip("/").strip()
    return cleaned or None


def _parse_ark_card(d: dict) -> dict[str, str] | None:
    """解析 json（ark）段的语义字段，供 XML 渲染与 excerpt 摘要两处复用。

    返回只含非空值的 dict（键：app / summary / title / desc / url），
    啥都解析不出（data 非法 JSON / 空对象）→ None。字段语义：
    - app     ark 应用标识（如 com.tencent.structmsg 链接分享、
              com.tencent.miniapp_01 小程序），LLM 可据此判断卡片种类
    - summary QQ 自己的外显文案（ark 顶层 prompt，如 "[QQ小程序]哔哩哔哩"），
              最稳的一句话摘要
    - title / desc  卡片标题与描述（meta.* 内首个命中的 title/desc；小程序
              卡片里 title 常是应用名、desc 是内容标题，照实透传不加工）
    - url     卡片跳转链接（qqdocurl > jumpUrl > url > musicUrl 择先）
    """
    raw = d.get("data")
    obj = None
    if isinstance(raw, dict):
        obj = raw
    elif isinstance(raw, str) and raw.strip():
        try:
            obj = json.loads(raw)
        except Exception:
            obj = None
    if not isinstance(obj, dict):
        return None

    card: dict[str, str] = {}
    app = obj.get("app")
    if isinstance(app, str) and app.strip():
        card["app"] = app.strip()
    prompt = obj.get("prompt")
    if isinstance(prompt, str) and prompt.strip():
        card["summary"] = prompt.strip()
    meta = obj.get("meta")
    if isinstance(meta, dict):
        for detail in meta.values():
            if not isinstance(detail, dict):
                continue
            if "title" not in card:
                title = _nonempty_str(detail.get("title"))
                if title:
                    card["title"] = title
            if "desc" not in card:
                desc = _nonempty_str(detail.get("desc"))
                if desc:
                    card["desc"] = desc
            if "url" not in card:
                url = (
                    _nonempty_str(detail.get("qqdocurl"))
                    or _nonempty_str(detail.get("jumpUrl"))
                    or _nonempty_str(detail.get("url"))
                    or _nonempty_str(detail.get("musicUrl"))
                )
                if url:
                    card["url"] = url
    return card or None


def _render_card_segment(d: dict) -> str:
    """json（ark）段 → ``<card/>``。napcat 接收侧一切富卡片——链接分享、
    B 站/小程序、公众号文章、位置、群推荐——都以 json 段到达，历史上渲染
    成 `<card format="json"/>` 等于把"别人分享了什么"整个丢掉。

    字段语义见 :func:`_parse_ark_card`（全部可缺省，缺失=该字段解析不到）。
    解析不出任何字段 → 回退 `<card format="json"/>`（format= 表示"未解析的
    原始段格式"；改名自 type=——与 notice/hint 的 kind= 用词不统一且语义含糊）。
    """
    card = _parse_ark_card(d)
    if not card:
        return '<card format="json"/>'
    attrs: list[str] = []
    for key, limit in (
        ("app", 60),
        ("summary", 100),
        ("title", 100),
        ("desc", 200),
        ("url", 300),
    ):
        value = card.get(key)
        if value:
            attrs.append(f'{key}="{_esc_attr(_clip(value, limit))}"')
    return f"<card {' '.join(attrs)}/>"


def _nonempty_str(value) -> str | None:
    """str 且去空白后非空才返回；其他类型（数字/dict）不硬转——ark 字段
    类型不受我们控制，硬转出 "{'x': 1}" 这种属性值比缺失更有歧义。"""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _append_name_attr(
    attrs: list[str], attr_name: str, user_id, names: dict[str, str]
) -> None:
    """user/operator/target 的 QQ 号能在近期消息里反查到名字时，紧跟着追加
    `<attr_name>="名字"` 属性。查不到就什么都不加（缺失=未知）。"""
    name = names.get(str(user_id))
    if name:
        attrs.append(f'{attr_name}="{_esc_attr(name)}"')


def _notice_detail_attrs(kind: str, payload: dict) -> list[str]:
    """按 notice kind 透出 mapper 已存储的明细字段（此前渲染时全部丢弃）。

    每个属性一种含义，不复用模糊名：
    - group_ban    → duration_seconds=禁言秒数（仅 sub_type=ban 且 >0 时给；
                     lift_ban / 解禁没有时长概念，不渲染）
    - group_card   → old_card= / new_card=（改名片前后值；空串=没有名片，
                     与"缺失=mapper 没拿到"区分开，所以空串也渲染）
    - group_upload → file_name= / file_size_bytes=
    - poke         → action=动作文案 + action_suffix=后缀（napcat raw_info
                     提炼，如 拍了拍…的头；缺失=普通戳一戳/未知）
    - emoji_like   → message_id=被贴表情的消息（对应 timeline 里同 id 的
                     <message>）+ likes=表情统计（格式见 _emoji_likes_label）
    - essence      → message_id=被设/取消精华的消息
    - honor        → honor_type=荣誉类型（talkative 龙王 / performer /
                     emotion，OneBot 原值透传）
    - group_recall / friend_recall
                   → message_id=被撤回的消息（对应 timeline 里同 id 的
                     <message>；没有它 LLM 只知道"有人撤回了"，不知道撤的
                     是哪条，会继续引用已撤回的内容）
    """
    attrs: list[str] = []
    if kind == "group_ban":
        duration = payload.get("duration")
        try:
            seconds = int(str(duration).strip())
        except (TypeError, ValueError):
            seconds = 0
        if seconds > 0:
            attrs.append(f'duration_seconds="{seconds}"')
    elif kind == "group_card":
        for key, attr in (("card_old", "old_card"), ("card_new", "new_card")):
            value = payload.get(key)
            if value is not None:
                attrs.append(f'{attr}="{_esc_attr(str(value))}"')
    elif kind == "group_upload":
        file_info = payload.get("file") or {}
        if isinstance(file_info, dict):
            fname = file_info.get("name")
            if fname:
                attrs.append(f'file_name="{_esc_attr(_clip(str(fname), 100))}"')
            fsize = file_info.get("size")
            if fsize is not None and str(fsize).strip():
                attrs.append(f'file_size_bytes="{_esc_attr(str(fsize))}"')
    elif kind == "poke":
        for key in ("action", "action_suffix"):
            value = payload.get(key)
            if value is not None and str(value).strip():
                attrs.append(f'{key}="{_esc_attr(str(value).strip())}"')
    elif kind == "emoji_like":
        mid = payload.get("onebot_message_id")
        if mid:
            attrs.append(f'message_id="{_esc_attr(str(mid))}"')
        label = _emoji_likes_label(payload.get("likes") or [])
        if label:
            attrs.append(f'likes="{_esc_attr(label)}"')
    elif kind == "essence":
        mid = payload.get("onebot_message_id")
        if mid:
            attrs.append(f'message_id="{_esc_attr(str(mid))}"')
    elif kind in ("group_recall", "friend_recall"):
        mid = payload.get("onebot_message_id")
        if mid:
            attrs.append(f'message_id="{_esc_attr(str(mid))}"')
    elif kind == "honor":
        honor_type = payload.get("honor_type")
        if honor_type:
            attrs.append(f'honor_type="{_esc_attr(str(honor_type))}"')
    return attrs


def _emoji_likes_label(likes) -> str | None:
    """把 emoji_like 的 likes 数组压成一个可读属性值，如 "👍×2,face:66×1"。

    每项 "表情×人数"，逗号分隔；表情的两种形态（napcat 的 emoji_id 两义）：
    - unicode 表情：emoji_id 是十进制 codepoint（128077 → 👍），直接给字符——
      LLM 读 "👍" 比读 "128077" 无歧义得多；
    - QQ 黄豆表情：emoji_id 是小整数 face id，渲染 "face:<id>"（与消息里
      <face face_id=.../> 同一 id 空间）。
    条目上限 5，防御异常 payload 撑爆属性。全部无效 → None（不渲染 likes=）。
    """
    parts: list[str] = []
    for item in list(likes)[:5]:
        if not isinstance(item, dict):
            continue
        symbol = _emoji_symbol(item.get("emoji_id"))
        if symbol is None:
            continue
        count = item.get("count")
        if count is not None and str(count).strip():
            parts.append(f"{symbol}×{count}")
        else:
            parts.append(symbol)
    return ",".join(parts) or None


def _emoji_symbol(emoji_id) -> str | None:
    """emoji_id → 显示符号。≥0x2000 视作 unicode codepoint 转字符（QQ 的
    unicode 类回应都在 emoji 区段，远高于黄豆 face id 的几百量级；排除
    surrogate 区），小整数按 QQ face id 渲染 "face:N"，非数字原样截断透传。"""
    if emoji_id is None:
        return None
    s = str(emoji_id).strip()
    if not s:
        return None
    try:
        value = int(s)
    except ValueError:
        return _clip(s, 20)
    if 0x2000 <= value <= 0x10FFFF and not (0xD800 <= value <= 0xDFFF):
        try:
            return chr(value)
        except ValueError:
            pass
    return f"face:{value}"


def _bracket(s: str) -> str:
    """gloss 用的 "[语义]" 占位包装：没带 "[" 前缀的才包（napcat 的 summary /
    faceText 常自带方括号，如 "[动画表情]"；商城表情名 "贴贴" 则是裸文本）。"""
    return s if s.startswith("[") else f"[{s}]"


def _segment_gloss(seg: dict) -> str | None:
    """单个 segment 的纯文本摘要，用于 reply 段的 excerpt。

    与 ``_render_segments`` 的 XML 渲染**同源取语义字段**——消息体里渲得出
    的信息（图片/商城表情 summary、face 名、ark 卡片外显文案、markdown 正文、
    文件名……），被回复时的 excerpt 里也要看得到；否则"回复一张表情包 / 一个
    B 站卡片"会退化成 [image] / [json] 类型占位，回复链语义断掉。两处新增
    段类型时必须同步改。

    样式对齐 QQ 会话列表习惯：非文本段用 "[语义]" 占位、@ 用 "@号"。
    返回 None = 该段对摘要无贡献（嵌套 reply 标记本身）。
    """
    t = seg.get("type")
    d = seg.get("data") or {}
    if t == "text":
        return str(d.get("text", ""))
    if t == "image":
        summary = str(d.get("summary") or "").strip()
        if summary:
            return _bracket(summary)
        return "[表情]" if _image_kind(d) == "sticker" else "[图片]"
    if t == "face":
        name = _face_name(d)
        return _bracket(name) if name else "[表情]"
    if t == "mface":
        summary = str(d.get("summary") or "").strip()
        return _bracket(summary) if summary else "[表情]"
    if t == "markdown":
        content = str(d.get("content") or "").strip()
        return content or "[markdown]"
    if t == "json":
        card = _parse_ark_card(d) or {}
        # prompt（QQ 外显文案）通常已是 "[QQ小程序]哔哩哔哩" 形态，原样用；
        # 没有 prompt 时退 title，标上 [卡片] 出处。
        if card.get("summary"):
            return card["summary"]
        if card.get("title"):
            return f"[卡片]{card['title']}"
        return "[卡片]"
    if t == "share":
        title = str(d.get("title") or "").strip()
        return f"[分享]{title}" if title else "[分享]"
    if t == "xml":
        return "[卡片]"
    if t == "file":
        fname = str(d.get("name", "") or d.get("file", "")).strip()
        return f"[文件]{fname}" if fname else "[文件]"
    if t == "record":
        return "[语音]"
    if t == "video":
        return "[视频]"
    if t == "at":
        qq = str(d.get("qq", "")).strip()
        if qq == "all":
            return "@全体成员"
        return f"@{qq}" if qq else "@?"
    if t == "dice":
        val = str(d.get("result", "") or d.get("value", "")).strip()
        return f"[骰子:{val}]" if val else "[骰子]"
    if t == "rps":
        val = str(d.get("result", "") or d.get("value", "")).strip()
        return f"[猜拳:{val}]" if val else "[猜拳]"
    if t == "poke":
        return "[戳一戳]"
    if t == "forward":
        return "[聊天记录]"
    if t == "reply":
        # 被回复消息自己又引用了别人——嵌套引用标记不进摘要，摘要只描述
        # 这条消息"说了什么"。
        return None
    return f"[{t}]" if t else None


def _gloss_segments(segments: Iterable) -> str:
    """段数组 → 单行摘要（前 40 字，超长加 "…"）。

    逐段取 ``_segment_gloss``：文本段取原文，富媒体段取与消息体渲染同源的
    语义占位（不是裸类型列表）。空白规整成单空格（摘要是单行属性值，不该
    带换行）。窗口内 excerpt 索引与 ingest 富化的 quoted.segments 共用这
    一条渲染路径，保证两个来源的 excerpt 形态逐字节同规格。
    """
    parts: list[str] = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        gloss = _segment_gloss(seg)
        if gloss:
            parts.append(gloss)
    excerpt = " ".join("".join(parts).split())
    if len(excerpt) > 40:
        excerpt = excerpt[:40] + "…"
    return excerpt


def _build_reply_task_guard(
    events: Iterable[_EventSnapshot],
) -> dict[str, dict]:
    """reply_task_id → {"max_revision": int, "cancelled": bool} 预扫索引。

    供 ``_completed_is_stale`` 判定过期完成事件（§1.5）：窗口内可见的最高
    upsert revision 与 cancel 事实。窗口外的更早历史不参与——守卫是防御层，
    写入侧的 scope 锁复核才是主防线。
    """
    guard: dict[str, dict] = {}
    for ev in events:
        if ev.type == "agent.reply_task_upserted":
            task_id = str((ev.payload or {}).get("reply_task_id") or "")
            if not task_id:
                continue
            revision = (ev.payload or {}).get("revision")
            entry = guard.setdefault(
                task_id, {"max_revision": 0, "cancelled": False}
            )
            if isinstance(revision, int) and revision > entry["max_revision"]:
                entry["max_revision"] = revision
        elif ev.type == "agent.reply_task_cancelled":
            task_id = str((ev.payload or {}).get("reply_task_id") or "")
            if not task_id:
                continue
            entry = guard.setdefault(
                task_id, {"max_revision": 0, "cancelled": False}
            )
            entry["cancelled"] = True
    return guard


def _completed_is_stale(
    ev: _EventSnapshot, guard: dict[str, dict]
) -> bool:
    """过期完成事件判定：revision 低于窗口内最高 upsert，或任务已 cancel。"""
    payload = ev.payload or {}
    task_id = str(payload.get("reply_task_id") or "")
    entry = guard.get(task_id)
    if entry is None:
        return False
    if entry["cancelled"]:
        return True
    revision = payload.get("revision")
    return (
        isinstance(revision, int)
        and entry["max_revision"] > 0
        and revision < entry["max_revision"]
    )


def _build_excerpt_index(events: Iterable[_EventSnapshot]) -> dict[str, str]:
    """timeline 内 onebot_message_id → 摘要（前 40 字）。

    用于渲染 reply 段时给 LLM 提供"被回复消息说了啥"的上下文。命中不到
    （消息在取数窗口外或被 napcat 抛弃了）时由 reply 段自身的
    quoted 富化兜底；两头都没有才渲染裸 reply.to_message_id。
    """
    out: dict[str, str] = {}
    for ev in events:
        if not ev.type.startswith("external.message."):
            continue
        mid = ev.payload.get("onebot_message_id")
        if not mid:
            continue
        excerpt = _gloss_segments(ev.payload.get("segments") or [])
        if excerpt:
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


@dataclass(frozen=True)
class _AuthorRef:
    """被回复消息的作者信息，供 reply 段拆成独立属性渲染。

    - ``name`` / ``user_id``：作者名（card/nickname 择先）与 QQ 号，任一可为
      None（=未知，渲染时省略对应属性，不造 "?" 占位）。
    - ``is_self``：该消息是 bot 自己发出的（来自 send_message 工具的
      tool_result）。这是**服务端事实标注**，独立于当 tick 是否拿得到
      bot_user_id——渲染成 `from_self="true"`，取代旧的 `from="我(...)"`
      魔法名字。
    """

    name: str | None
    user_id: str | None
    is_self: bool = False


def _build_author_index(
    events: Iterable[_EventSnapshot],
) -> dict[str, _AuthorRef]:
    """onebot_message_id → :class:`_AuthorRef`。用于 reply 段标
    from_name / from_qq / from_self 三个独立属性。

    覆盖两类来源：
    - 外部消息：作者 = sender 的 card/nickname + user_id。别人引用某人时，
      LLM 据此判断被引用的是谁（含群主自己）——而不是误以为那人在发言。
    - bot 自己发出的消息：``is_self=True``。现役来源是 ``send_messages``
      终态里 confirmed-sent 的逐条 receipt（tool_result 的 result 或
      tool_failed 平铺 payload 里的 ``sent_messages``，partial 时只收
      status="sent" 项）；历史来源是旧链路 runtime.reply_flushed 的成功
      sent item。别人引用 bot 时渲染 `from_self="true"`。

    单值 ``result.message_id`` 分支只作 append-only 历史兼容：旧
    send_message 与旧 meme.send/send_meme 的 tool_result 仍可恢复
    from_self；现役 reply tool_result 不含 message_id，被门槛自然滤掉。
    名字元组里的 ``meme`` 同样是**历史名**（2026-07-25 改名
    ``meme_collection`` 且无 send 动作）。
    """
    out: dict[str, _AuthorRef] = {}
    # 收集发言工具调用 id（现役 send_messages + 历史名）。
    send_call_ids: set[str] = set()
    for ev in events:
        if ev.type == "agent.tool_called" and (
            (ev.payload or {}).get("tool_name")
            in ("send_messages", "send_message", "meme", "send_meme", "reply")
        ):
            tc_id = (ev.payload or {}).get("tool_call_id")
            if tc_id is not None:
                send_call_ids.add(str(tc_id))

    def _harvest_sent_items(source: dict) -> None:
        for item in source.get("sent_messages") or []:
            if not isinstance(item, dict) or item.get("status") != "sent":
                continue
            mid = item.get("message_id")
            if mid is None:
                continue
            self_id = item.get("self_id")
            out[str(mid)] = _AuthorRef(
                name=None,
                user_id=str(self_id) if self_id else None,
                is_self=True,
            )

    for ev in events:
        if ev.type == "runtime.reply_flushed":
            _harvest_sent_items(ev.payload or {})
        # bot 自己经工具发出的消息：从终态取 message_id + self_id。
        # tool_result → receipts 在 result（send_messages）或单值
        # message_id（历史工具）；tool_failed → partial 的 receipts 经
        # extra 平铺在 payload 顶层。
        if ev.type in ("agent.tool_result", "agent.tool_failed"):
            payload = ev.payload or {}
            tc_id = payload.get("tool_call_id")
            if tc_id is not None and str(tc_id) in send_call_ids:
                if ev.type == "agent.tool_result":
                    result = payload.get("result") or {}
                    if isinstance(result, dict):
                        _harvest_sent_items(result)
                        mid = result.get("message_id")
                        if mid is not None:
                            self_id = result.get("self_id")
                            out[str(mid)] = _AuthorRef(
                                name=None,
                                user_id=str(self_id) if self_id else None,
                                is_self=True,
                            )
                else:
                    _harvest_sent_items(payload)
        # 外部消息作者。
        if not ev.type.startswith("external.message."):
            continue
        mid = ev.payload.get("onebot_message_id")
        if not mid:
            continue
        sender = ev.payload.get("sender") or {}
        name = sender.get("card") or sender.get("nickname")
        qq = sender.get("user_id") or ev.user_id
        out[str(mid)] = _AuthorRef(
            name=str(name) if name else None,
            user_id=str(qq) if qq is not None else None,
        )
    return out

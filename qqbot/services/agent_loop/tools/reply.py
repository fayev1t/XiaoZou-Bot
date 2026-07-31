"""reply 工具：发言两步中的第一步——保存局势解析并启动等待。

**2026-07-31 删除 Replyer**（重构提案-删除Replyer.md）：本工具的参数与存储
语义不变（append-only、latest-revision-wins、find-open-or-create、
hard_deadline、cancel），但下游换了人。等待到点后 ReplyExecutor 不再组稿发
送，而是写一条 ``runtime.reply_task_completed``（携带完整 analysis）并立即
唤醒 Planner；那一拍由 Planner 自己结合最新时间流决定是否调 ``send_messages``
以及最终措辞。analysis 从"跨模型交接"变成 Planner 跨拍保存的自我备忘，字段
含义不变：它是对人物指向、话题线、时间节点、待回答内容与可靠事实的解析结果，
不是最终可见文案，也不承载 messages。

**2026-07-28 分工收敛**：追加只收一个自由文本 ``analysis`` 加一个
``hold_seconds``。

砍掉的是 `targets[] {message_id, sender_qq, context, guidance}` +
`gist {situation, intent, facts, avoid, tone}` 那套结构化槽位。它们**从来不
是机器契约**——程序侧除了一条"不能为空"的兜底之外一个字段都不消费；到点
交接需要的是完整判读，而不是固定槽位。既然只是提示词交接形状，
就该按"能不能把局势讲清楚"评判，而九个槽位在这上面是负分：
  - `additionalProperties: false` 把辅助维度封死——人物之间的引用/@关系、并行
    话题和关键先后顺序没有稳定槽位，只能被硬塞进 context/gist，语义降级；
  - 槽位诱导填充——`gist.situation` 与 `targets[].context` 天然重叠、
    `intent` 与 `guidance` 天然重叠，一个两句话的判读被切成七份写；
  - 与 append-only 语义打架——授权是追加的、最新一条为准，但结构化槽位一追加
    就有"第二条的空 facts 是不是撤销了第一条的"这种歧义，只能靠文档打补丁。
一段自然语言没有这三个问题：想说几个维度写几个维度，"最新一条为准"也天然成立。
被回应消息的 message_id 不再单列字段（需要消歧时直接写进 analysis；落笔那一拍
从同一份 timeline 复制真实 id）。

**`action` 保留，但改成可选**：省略 = 追加一条授权（普通发言的唯一形态），
`"cancel"` 撤稿。取消的是 `"upsert"` 这个取值——2026-07-24 改 append-only 之后
它就名不副实了（那之前它真是 upsert：带 reply_task_id + expected_revision 做
CAS 合并），留着只是让每次正常发言都先声明一遍状态机操作，把一个纯粹的表达
行为包装成状态迁移。

**`action="verbatim"` 已于 2026-07-30 删除**：当时的理由是双模型分工不该留
后门。2026-07-31 删除 Replyer 后"绕过谁"已无从谈起，但**本工具仍然不承载
最终字句**——它的产出是解析与等待，可见文字只经 ``send_messages`` 发出；
`messages` / `verbatim_messages` 等旧载荷字段继续按迁移 reason_code 拒绝，
不静默降级。

追加仍是 **append-only**（2026-07-24，待办#19）：每次调用都是完整、自足的一
条，不引用上一次、程序也不做任何字段合并。scope 内仍只有一份 open reply_task；
后续调用 append 新 revision，并让最新 revision 的完整 analysis 与 flush_at 直接
获胜（能延长也能缩短）。旧 revision 留在事件流供审计与回看；到点交接只携带折叠
态里的最新 analysis，不做“旧内容未冲突部分继续生效”的隐式语义合并。历史
``brief`` 领域事件由折叠层兼容读取，但旧工具参数会 fail loudly，促使 Planner
按新边界重发。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from qqbot.core.ids import new_event_id
from qqbot.core.logging import get_logger
from qqbot.core.time import china_now
from qqbot.services.agent_loop.prompts import load_sibling_md
from qqbot.services.agent_loop.reply_task import (
    MAX_HOLD_SECONDS,
    append_cancel,
    append_upsert,
    build_upsert_payload,
    find_cancel_for_tool_call,
    find_upsert_for_tool_call,
    load_open_reply_task,
    scope_lock,
)
from qqbot.services.agent_loop.tool_registry import BaseTool, ToolOutcome

_USAGE_PROMPT = load_sibling_md(__file__, "reply.md")
logger = get_logger(__name__)

# 退役字段：旧形态的调用不静默吃掉，按字段给可读 reason_code，让 Planner 下一
# 拍照错误自纠（沿用 2026-07-22 `points_replaced_by_context` 的先例）。静默忽略
# 是最坏的处理——模型会以为那套判读已经存进了草稿，而从 <result> 上看不出异常。
_ANALYSIS_HINT = (
    "targets/gist are retired: synthesize the participants and addressee "
    "relationships, topic threads, decisive time/order changes, unresolved "
    "content, verified facts and uncertainty as one plain-language `analysis`"
)
# 逐字直发（action="verbatim" + messages）2026-07-30 整条删除，见模块 docstring。
_VERBATIM_GONE = (
    "verbatim_removed",
    "this tool never carries final message text: store the `analysis` and "
    "wait; when <reply-task-completed> returns, choose the wording and send "
    "it with the send_messages tool",
)
_RETIRED_KEYS: dict[str, tuple[str, str]] = {
    "brief": (
        "brief_renamed_to_analysis",
        "brief was renamed to analysis and its old style-steering semantics "
        "were removed",
    ),
    "targets": ("targets_gist_replaced_by_analysis", _ANALYSIS_HINT),
    "gist": ("targets_gist_replaced_by_analysis", _ANALYSIS_HINT),
    "points": ("targets_gist_replaced_by_analysis", _ANALYSIS_HINT),
    "mode": (
        "mode_removed",
        "there is no mode; ordinary speech needs no action at all and "
        "withdrawing is action=\"cancel\"",
    ),
    # messages / verbatim_messages 都是已删除的逐字直发通道的载荷字段
    # （2026-07-30）。分开给码，是为了让模型从 <result> 上直接看出"不是字段
    # 名写错了，是这条路没有了"，而不是去猜正确的拼法再试一次。
    "messages": _VERBATIM_GONE,
    "verbatim_messages": _VERBATIM_GONE,
    "expected_revision": (
        "expected_revision_removed",
        "revisions are not CAS-checked; every call appends",
    ),
}


class ReplyTool(BaseTool):
    name = "reply"
    allowed_scopes = ("group", "private")
    description = (
        "Step one of speaking: store your resolved map of the situation and "
        "start waiting. Normally you pass exactly `analysis` — participants "
        "and addressee relationships, topic threads, decisive chronology, "
        "unresolved content and reliable facts — plus `hold_seconds`. That "
        "appends one self-contained revision to this scope's pending draft; "
        "each call stands alone and the newest analysis and hold_seconds "
        "replace the previous revision outright. This call sends NOTHING and "
        "carries no message text. When the wait ends, the analysis returns to "
        "the timeline as <reply-task-completed> and wakes you; that tick you "
        "re-read the latest timeline and decide the actual wording with "
        "send_messages — or decide the moment has passed and stay silent. "
        'One rare branch: action="cancel" withdraws the pending draft.'
    )
    usage_prompt = _USAGE_PROMPT
    arguments_schema = {
        "type": "object",
        "properties": {
            "analysis": {
                "type": "string",
                "description": (
                    "Free-text resolved conversation analysis — a memo your "
                    "later self reads inside <reply-task-completed>: who is "
                    "talking to/quoting/@-ing whom, the relevant social "
                    "relationship when evidenced, active topic threads, "
                    "decisive time/order changes, the exact unresolved "
                    "question/claim/request, verified facts and uncertainty, "
                    "plus unrelated or already-resolved threads to exclude. "
                    "Synthesize conclusions instead of merely pointing at the "
                    "timeline. It is analysis, not draft text — final wording "
                    "is chosen later in send_messages."
                ),
            },
            "hold_seconds": {
                "type": "integer",
                "minimum": 0,
                "maximum": MAX_HOLD_SECONDS,
                "description": (
                    "Required when appending — there is no default. How long "
                    "to hold this draft before the wait completes and the "
                    "analysis returns as <reply-task-completed>. The newest "
                    "call wins outright: it may shorten as well as extend."
                ),
            },
            "action": {
                "type": "string",
                "enum": ["cancel"],
                "description": (
                    "Omit it for ordinary speech — that is what this tool "
                    "does. `cancel` withdraws the pending draft entirely."
                ),
            },
            "reply_task_id": {
                "type": "string",
                "description": (
                    'action="cancel" only, and optional even there: omit to '
                    "withdraw whatever draft is pending. If given it must "
                    "match, so a stale id fails loudly instead of silently "
                    "cancelling a different draft."
                ),
            },
        },
        "required": [],
        "additionalProperties": False,
        "allOf": [
            {
                "if": {"not": {"required": ["action"]}},
                "then": {
                    "required": ["analysis", "hold_seconds"],
                    "not": {"required": ["reply_task_id"]},
                },
            },
            {
                "if": {
                    "properties": {"action": {"const": "cancel"}},
                    "required": ["action"],
                },
                "then": {
                    "not": {
                        "anyOf": [
                            {"required": ["analysis"]},
                            {"required": ["hold_seconds"]},
                        ]
                    }
                },
            },
        ],
    }

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        if not isinstance(arguments, dict):
            return _invalid(
                "arguments_not_object", "reply arguments must be a JSON object"
            )
        if fail := await self.enforce_access(context):
            return fail
        scope_key = context.get("scope_key")
        session_factory = context.get("session_factory")
        correlation_id = context.get("correlation_id")
        tool_call_event_id = context.get("tool_call_event_id")
        if not all(
            isinstance(value, str) and value
            for value in (scope_key, correlation_id, tool_call_event_id)
        ) or session_factory is None:
            return ToolOutcome.failure(
                "internal_tool_error", "reply task persistence is not wired"
            )
        if fail := _reject_retired(arguments):
            return fail
        if fail := _reject_unknown(arguments):
            return fail

        action = arguments.get("action")
        if "action" in arguments and action != "cancel":
            if action == "upsert":
                return _invalid(
                    "upsert_removed",
                    "there is no upsert action: omit `action` entirely to "
                    "append an authorization",
                )
            if action == "verbatim":
                return _invalid(*_VERBATIM_GONE)
            return _invalid("bad_action", 'action must be omitted or "cancel"')
        if action != "cancel" and "reply_task_id" in arguments:
            # 带 id 来追加 = 还在用旧的"指名改某一份稿"心智模型。静默忽略会让它
            # 以为自己精确命中了某份稿，实际是往当前 open 的那份上追加。
            return _invalid(
                "reply_task_id_needs_cancel",
                "reply_task_id only applies to action=\"cancel\"; every other "
                "call appends to the scope's one pending draft, so there is "
                "nothing to name",
            )
        if action == "cancel":
            extras = sorted(set(arguments) - {"action", "reply_task_id"})
            if extras:
                return _invalid(
                    "cancel_arguments_not_applicable",
                    "action=\"cancel\" accepts only optional reply_task_id; "
                    f"remove: {', '.join(extras)}",
                )
        async with scope_lock(scope_key):
            if action == "cancel":
                return await self._cancel(
                    arguments,
                    session_factory=session_factory,
                    scope_key=scope_key,
                    correlation_id=correlation_id,
                    tool_call_event_id=tool_call_event_id,
                )
            return await self._append(
                arguments,
                session_factory=session_factory,
                scope_key=scope_key,
                correlation_id=correlation_id,
                tool_call_event_id=tool_call_event_id,
                notify=context.get("notify_reply_task"),
            )

    async def _append(
        self,
        arguments: dict,
        *,
        session_factory: Any,
        scope_key: str,
        correlation_id: str,
        tool_call_event_id: str,
        notify: Any,
    ) -> ToolOutcome:
        # ToolWorker 在 domain event 已写、tool_result 未写之间崩溃时的幂等恢复。
        existing_payload = await find_upsert_for_tool_call(
            session_factory, tool_call_event_id
        )
        if existing_payload is not None:
            return ToolOutcome.success(_result_from_payload(existing_payload))

        analysis, hold, fail = _validate_append(arguments)
        if fail:
            return fail

        # ─── append-only（2026-07-24，待办#19）───
        # 每次调用就是一条完整、自足的解析，不引用上一次、不做字段合并：
        # 模型不再抄 reply_task_id/expected_revision，程序也不再 merge 内容
        # （旧 merge 只增不删，撤不掉写错的判读与事实）。scope 内仍只有一份
        # open reply_task 承载"等到什么时候"，后续调用 append 上去、把
        # flush_at 换成自己的——**最新一次调用直接获胜，能延长也能缩短**。
        # 旧 revision 仍以 <tool-call name="reply"> 留在 timeline 供回看；到点
        # 交接从 ReplyTaskState 折叠态直接拿最新 analysis 写进完成事件，避免
        # tool_result 终态竞态与窗口裁剪。
        now = china_now()
        current = await load_open_reply_task(session_factory, scope_key)
        if current is None:
            task_id = new_event_id()
            revision = 1
            created_at = now
            hard_deadline = now + timedelta(seconds=MAX_HOLD_SECONDS)
        else:
            task_id = current.reply_task_id
            revision = current.revision + 1
            created_at = current.created_at
            hard_deadline = current.hard_deadline
        # hard_deadline 自创建时刻起算、不随 append 滑动——它是"再怎么等也
        # 必须发出去"的硬上界。
        flush_at = min(now + timedelta(seconds=hold), hard_deadline)

        payload = build_upsert_payload(
            reply_task_id=task_id,
            revision=revision,
            created_at=created_at,
            updated_at=now,
            flush_at=flush_at,
            hard_deadline=hard_deadline,
            analysis=analysis,
        )
        event_id = await append_upsert(
            session_factory,
            scope_key=scope_key,
            correlation_id=correlation_id,
            tool_call_event_id=tool_call_event_id,
            payload=payload,
        )
        if notify is not None:
            try:
                await notify(scope_key, task_id, revision, flush_at, event_id)
            except Exception as exc:
                # 落稿事件已经是完成真值；调度通知失败不能把已成功的工具调用
                # 反写成失败。后续拍仍能从 timeline 上自己那条 <tool-call
                # name="reply"> 行看见并追授权/撤销，重启 rescan 也会重挂未来
                # 定时器。
                logger.warning(
                    "[reply] persisted task {} but scheduling failed: {}",
                    task_id,
                    exc,
                )
        return ToolOutcome.success(_result_from_payload(payload))

    async def _cancel(
        self,
        arguments: dict,
        *,
        session_factory: Any,
        scope_key: str,
        correlation_id: str,
        tool_call_event_id: str,
    ) -> ToolOutcome:
        existing = await find_cancel_for_tool_call(
            session_factory, tool_call_event_id
        )
        if existing is not None:
            return ToolOutcome.success(
                {
                    "reply_task_id": existing.get("reply_task_id"),
                    "revision": existing.get("revision"),
                    "state": "cancelled",
                }
            )
        # reply_task_id 可省（2026-07-24，待办#19）：scope 内至多一份 open
        # reply_task，"撤掉待发的那份"无歧义。给了就必须对得上——防止模型拿
        # 着一份已经发出去的旧 id 来撤，静默撤错另一份。expected_revision 随
        # append 语义一并取消：授权是追加的，没有需要 CAS 的合并冲突。
        requested_id = arguments.get("reply_task_id")
        if requested_id is not None and (
            not isinstance(requested_id, str) or not requested_id
        ):
            return _invalid("bad_reply_task_id", "reply_task_id must be a string")
        task = await load_open_reply_task(session_factory, scope_key)
        if task is None or (
            requested_id is not None and task.reply_task_id != requested_id
        ):
            return ToolOutcome.failure(
                "reply_task_not_found", "no open reply_task to cancel"
            )
        task_id = task.reply_task_id
        await append_cancel(
            session_factory,
            scope_key=scope_key,
            correlation_id=correlation_id,
            tool_call_event_id=tool_call_event_id,
            task=task,
        )
        return ToolOutcome.success(
            {
                "reply_task_id": task_id,
                "revision": task.revision,
                "state": "cancelled",
            }
        )


def _validate_append(arguments: dict) -> tuple[str, int, ToolOutcome | None]:
    """append 路径的参数校验：一段非空 analysis + 一个 hold_seconds，没有别的
    形态（逐字直发 2026-07-30 删除，`messages` 走 _RETIRED_KEYS 拒绝）。"""
    analysis = arguments.get("analysis")
    if not isinstance(analysis, str) or not analysis.strip():
        return (
            "",
            0,
            _invalid(
                "bad_analysis",
                "analysis must be a non-empty string — resolve who is talking "
                "to whom, the topic and chronology, and what remains to answer",
            ),
        )
    # hold_seconds 无默认值（2026-07-24，待办#19）：等多久是每次调用都要现场
    # 判断的语义（"这个人说完了没"），给默认值等于替模型做决定——曾经的
    # default 0 就把合并窗口整个关掉了。
    if "hold_seconds" not in arguments:
        return (
            "",
            0,
            _invalid(
                "missing_hold_seconds",
                "hold_seconds is required; there is no default",
            ),
        )
    hold = _coerce_hold(arguments.get("hold_seconds"))
    if hold is None:
        return "", 0, _bad_hold()
    return analysis.strip(), hold, None


def _reject_retired(arguments: dict) -> ToolOutcome | None:
    for key, (reason_code, message) in _RETIRED_KEYS.items():
        if key in arguments:
            return _invalid(reason_code, f"reply.{key} is retired: {message}")
    return None


def _reject_unknown(arguments: dict) -> ToolOutcome | None:
    known = {"action", "analysis", "hold_seconds", "reply_task_id"}
    extras = sorted(set(arguments) - known)
    if not extras:
        return None
    return _invalid(
        "unexpected_argument",
        f"reply received unknown argument(s): {', '.join(extras)}",
    )


def _coerce_hold(raw: Any) -> int | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int) and 0 <= raw <= MAX_HOLD_SECONDS:
        return raw
    if isinstance(raw, str) and raw.strip().isdigit():
        value = int(raw.strip())
        return value if 0 <= value <= MAX_HOLD_SECONDS else None
    return None


def _bad_hold() -> ToolOutcome:
    return _invalid(
        "bad_hold_seconds",
        f"hold_seconds must be an integer in [0, {MAX_HOLD_SECONDS}]",
    )


def _invalid(reason_code: str, message: str) -> ToolOutcome:
    return ToolOutcome.failure(
        "invalid_arguments",
        message,
        reason_code=reason_code,
        retryable=False,
        transient=False,
        user_fixable=True,
    )


def _result_from_payload(payload: dict) -> dict:
    """成功结果 = 这份稿的身份与调度事实。**永远没有 message_id**——落稿不是
    发言；到点后它折成 <reply-task-completed>，真发出去了只体现在后续
    send_messages 调用行的逐条回执上。"""
    return {
        "reply_task_id": payload.get("reply_task_id"),
        "revision": payload.get("revision"),
        "state": payload.get("state", "open"),
        "flush_at": payload.get("flush_at"),
        "hard_deadline": payload.get("hard_deadline"),
    }

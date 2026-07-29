"""reply 工具：当前 scope 唯一的发言入口。

**2026-07-28 分工收敛**：compose 授权只接收一个自由文本 ``analysis``，加一个
``hold_seconds``。analysis 是 Planner 对人物指向、话题线、时间节点、待回答内容
与可靠事实的解析结果，不是给 Replyer 的语气、情感、姿态或措辞指令。

砍掉的是 `targets[] {message_id, sender_qq, context, guidance}` +
`gist {situation, intent, facts, avoid, tone}` 那套结构化槽位。它们**从来不
是机器契约**——程序侧除了一条"compose 不能为空"的兜底之外一个字段都不消费；
Replyer 需要的是 Planner 的完整判读，而不是固定槽位。既然只是提示词交接形状，
就该按"能不能帮它把局势讲清楚"评判，而九个槽位在这上面是负分：
  - `additionalProperties: false` 把辅助维度封死——人物之间的引用/@关系、并行
    话题和关键先后顺序没有稳定槽位，只能被硬塞进 context/gist，语义降级；
  - 槽位诱导填充——`gist.situation` 与 `targets[].context` 天然重叠、
    `intent` 与 `guidance` 天然重叠，一个两句话的判读被切成七份写；
  - 与 append-only 语义打架——授权是追加的、最新一条为准，但结构化槽位一追加
    就有"第二条的空 facts 是不是撤销了第一条的"这种歧义，只能靠文档打补丁。
一段自然语言没有这三个问题：想说几个维度写几个维度，"最新一条为准"也天然成立。
被回应消息的 message_id 不再单列字段（需要消歧时直接写进 analysis；Replyer 从
同一份 timeline 复制真实 id）。

**`action` 保留，但改成可选**：省略 = 追加一条授权（普通发言的唯一形态），
`"cancel"` 撤稿，`"verbatim"` 逐字直发。取消的是 `"upsert"` 这个取值——
2026-07-24 改 append-only 之后它就名不副实了（那之前它真是 upsert：带
reply_task_id + expected_revision 做 CAS 合并），留着只是让每次正常发言都先
声明一遍状态机操作，把一个纯粹的表达行为包装成状态迁移。

**为什么不把 cancel / verbatim 拆成独立工具**（2026-07-25 评估后否决）：它们
确实是另外两件事，但工具是**目录级**的东西——每注册一个，Planner 每拍都要读
它的 catalog 条目（name/description/arguments_schema）和整段 usage 文档。代价
不只是 prompt 体积：`verbatim` 本该是"Replyer 挂了才走"的逃生路径，一旦升格成
与 `reply` 平级的工具，它的显著性就和日常发言一样高，等于主动邀请模型绕过
Replyer 和角色卡说话。留在 action 分支里，它的可见度才与它应有的使用频率匹配。

授权仍是 **append-only**（2026-07-24，待办#19）：每次调用都是完整、自足的一
条，不引用上一次、程序也不做任何字段合并。scope 内仍只有一份 open reply_task；
后续调用 append 新 revision，并让最新 revision 的完整 analysis 与 flush_at 直接
获胜（能延长也能缩短）。旧 revision 留在事件流供审计与 Planner 回看，但 Replyer
只消费折叠态里的最新 analysis，不再做“旧授权未冲突部分继续生效”的隐式语义
合并。历史 ``brief`` 领域事件由折叠层兼容读取，但旧工具参数会 fail loudly，促使
Planner 按新边界重发。
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
from qqbot.services.agent_loop.tools.send_message import _validate_content

_USAGE_PROMPT = load_sibling_md(__file__, "reply.md")
logger = get_logger(__name__)

MAX_VERBATIM_MESSAGES = 4

# 退役字段：旧形态的调用不静默吃掉，按字段给可读 reason_code，让 Planner 下一
# 拍照错误自纠（沿用 2026-07-22 `points_replaced_by_context` 的先例）。静默忽略
# 是最坏的处理——模型会以为那套判读送到了 Replyer，而从 <result> 上看不出异常。
_ANALYSIS_HINT = (
    "targets/gist are retired: synthesize the participants and addressee "
    "relationships, topic threads, decisive time/order changes, unresolved "
    "content, verified facts and uncertainty as one plain-language `analysis`; "
    "do not prescribe tone, emotion, persona, posture or final wording"
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
        "mode_replaced_by_action",
        'there is no mode; exact bytes are action="verbatim"',
    ),
    "verbatim_messages": (
        "verbatim_messages_renamed_to_messages",
        'exact bytes go in `messages` alongside action="verbatim"',
    ),
    "expected_revision": (
        "expected_revision_removed",
        "revisions are not CAS-checked; every call appends",
    ),
}


class ReplyTool(BaseTool):
    name = "reply"
    allowed_scopes = ("group", "private")
    # reply-only 成功批次不应为“草稿已落定”再开一拍；失败仍需让 Planner 看见。
    # cancel 同理：撤稿后没有 flush 会来，为"我决定不说了"再开一拍只会引诱模型
    # 立刻改主意重新落稿。
    wake_policy = "on_failure"
    description = (
        "Speak. Normally you pass exactly `analysis` — your resolved map of "
        "the participants, addressee relationships, topic threads, decisive "
        "chronology, unresolved content and reliable facts — plus "
        "`hold_seconds`. That appends one self-contained authorization "
        "to this scope's pending draft. Each call stands alone; never "
        "reference an earlier one; the newest analysis and hold_seconds replace "
        "the previous revision outright. "
        "Do not prescribe tone, emotion, persona, posture, meme use, bubble "
        "shape or final wording; the Replyer owns every expressive choice. "
        "A successful call only stores pending intent; it does NOT mean "
        "anything was sent — actual speech appears later as <my-reply> after "
        'runtime.reply_flushed. Two rare branches: action="cancel" withdraws '
        'the pending draft, action="verbatim" sends exact bytes bypassing the '
        "Replyer (escape hatch — it costs you the account's voice)."
    )
    usage_prompt = _USAGE_PROMPT
    arguments_schema = {
        "type": "object",
        "properties": {
            "analysis": {
                "type": "string",
                "description": (
                    "Free-text resolved conversation analysis for the Replyer: "
                    "who is talking to/quoting/@-ing whom, the relevant social "
                    "relationship when evidenced, active topic threads, "
                    "decisive time/order changes, the exact unresolved "
                    "question/claim/request, verified facts and uncertainty, "
                    "plus unrelated or already-resolved threads to exclude. "
                    "Synthesize the conclusions instead of merely pointing at "
                    "the shared timeline. Do not prescribe final wording, "
                    "tone, emotion, persona, conversational posture, humor, "
                    "meme use, bubble count or any other presentation choice."
                ),
            },
            "hold_seconds": {
                "type": "integer",
                "minimum": 0,
                "maximum": MAX_HOLD_SECONDS,
                "description": (
                    "Required when appending — there is no default. How long "
                    "to hold this draft before it is composed and sent. The "
                    "newest call wins outright: it may shorten as well as "
                    'extend. Optional on action="verbatim" (defaults to 0, '
                    "send now)."
                ),
            },
            "action": {
                "type": "string",
                "enum": ["cancel", "verbatim"],
                "description": (
                    "Omit it for ordinary speech — that is what this tool "
                    "does. `cancel` withdraws the pending draft entirely; "
                    "`verbatim` sends `messages` exactly as written, "
                    "bypassing the Replyer."
                ),
            },
            "messages": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_VERBATIM_MESSAGES,
                "description": (
                    'action="verbatim" only: 1-4 messages sent in order, each '
                    "with OneBot v11 content segments (text / at / reply / "
                    "face only; a reply segment is at most one and must come "
                    "first)."
                ),
                "items": {
                    "type": "object",
                    "properties": {"content": {"type": "array"}},
                    "required": ["content"],
                    "additionalProperties": False,
                },
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
                    "not": {
                        "anyOf": [
                            {"required": ["messages"]},
                            {"required": ["reply_task_id"]},
                        ]
                    },
                },
            },
            {
                "if": {
                    "properties": {"action": {"const": "verbatim"}},
                    "required": ["action"],
                },
                "then": {
                    "required": ["messages"],
                    "not": {
                        "anyOf": [
                            {"required": ["analysis"]},
                            {"required": ["reply_task_id"]},
                        ]
                    },
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
                            {"required": ["messages"]},
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
        if "action" in arguments and action not in ("cancel", "verbatim"):
            if action == "upsert":
                return _invalid(
                    "upsert_removed",
                    "there is no upsert action: omit `action` entirely to "
                    "append an authorization",
                )
            return _invalid(
                "bad_action", 'action must be omitted, "cancel" or "verbatim"'
            )
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
                verbatim=action == "verbatim",
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
        verbatim: bool,
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

        analysis, messages, hold, fail = _validate_append(arguments, verbatim)
        if fail:
            return fail

        # ─── append-only 授权（2026-07-24，待办#19）───
        # 每次调用就是一条完整、自足的授权，不引用上一次、不做字段合并：
        # 模型不再抄 reply_task_id/expected_revision，程序也不再 merge 内容
        # （旧 merge 只增不删，撤不掉写错的判读与事实）。scope 内仍只有一份
        # open reply_task 承载"等到什么时候发"，后续调用 append 上去、把
        # flush_at 换成自己的——**最新一次调用直接获胜，能延长也能缩短**。
        # 最新 revision 的 analysis 就是完整当前授权。旧 revision 仍以
        # <tool-call name="reply"> 留在 Planner timeline 供回看，但 Replyer 从
        # ReplyTaskState 直接拿最新 analysis，避免 tool_result 终态竞态与窗口裁剪。
        now = china_now()
        current = await load_open_reply_task(session_factory, scope_key)
        if current is None:
            task_id = new_event_id()
            revision = 1
            created_at = now
            hard_deadline = now + timedelta(seconds=MAX_HOLD_SECONDS)
        elif verbatim or current.mode != "compose":
            # verbatim 独占：它绕过 Replyer 直发，没有"综合多条授权"可言，
            # "逐字"语义也不该被别的授权改写。两个方向都拦——挂着 verbatim 时
            # 后续 append 被拒，挂着 compose 时 verbatim 也不能插进来。
            return ToolOutcome.failure(
                "reply_task_locked",
                "a verbatim draft is exclusive; cancel it first"
                if current.mode != "compose"
                else "a draft is already pending; cancel it before sending "
                "verbatim bytes",
                reply_task_id=current.reply_task_id,
            )
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
            mode="verbatim" if verbatim else "compose",
            analysis=analysis,
            verbatim_messages=messages,
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


def _validate_append(
    arguments: dict, verbatim: bool
) -> tuple[str, list[dict], int, ToolOutcome | None]:
    """append 路径的参数校验：compose 要 analysis，verbatim 要 messages。

    两者互斥且都不静默容忍对方的字段——verbatim 绕过 Replyer，写了 analysis 也
    没有任何读者，静默丢掉正是最难被发现的坏法。
    """
    if verbatim:
        if "analysis" in arguments:
            return (
                "",
                [],
                0,
                _invalid(
                    "analysis_not_applicable",
                    "verbatim bypasses the Replyer, so nothing reads analysis; "
                    "put the exact wording in `messages`",
                ),
            )
        messages, fail = _validate_messages(arguments.get("messages"))
        if fail:
            return "", [], 0, fail
        # hold 在 verbatim 上可省、默认 0（立刻发）。等待窗口的用处是让 Replyer
        # 在 flush 时把这期间的新消息折进来，而逐字直发根本不经 Replyer、字节
        # 已经定死，多等一秒不会让内容更贴切。
        if "hold_seconds" not in arguments:
            return "", messages, 0, None
        hold = _coerce_hold(arguments.get("hold_seconds"))
        if hold is None:
            return "", [], 0, _bad_hold()
        return "", messages, hold, None

    if "messages" in arguments:
        return (
            "",
            [],
            0,
            _invalid(
                "messages_need_verbatim",
                'messages only apply to action="verbatim"; ordinary speech '
                "hands the Replyer an `analysis` instead",
            ),
        )
    analysis = arguments.get("analysis")
    if not isinstance(analysis, str) or not analysis.strip():
        return (
            "",
            [],
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
            [],
            0,
            _invalid(
                "missing_hold_seconds",
                "hold_seconds is required; there is no default",
            ),
        )
    hold = _coerce_hold(arguments.get("hold_seconds"))
    if hold is None:
        return "", [], 0, _bad_hold()
    return analysis.strip(), [], hold, None


def _validate_messages(raw: Any) -> tuple[list[dict], ToolOutcome | None]:
    """1..4 条，每条的 content 走 send_message 的严格段校验（text/at/reply/
    face，reply 段至多一个且必须首位）。"""
    if not isinstance(raw, list) or not raw:
        return [], _invalid(
            "empty_messages", 'action="verbatim" requires at least one message'
        )
    if len(raw) > MAX_VERBATIM_MESSAGES:
        return [], _invalid(
            "too_many_messages",
            f"at most {MAX_VERBATIM_MESSAGES} verbatim messages",
        )
    out: list[dict] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            return [], _invalid(
                "bad_message", f"messages[{index}] must be an object"
            )
        extras = sorted(set(item) - {"content"})
        if extras:
            return [], _invalid(
                "unexpected_message_argument",
                f"messages[{index}] accepts only content; remove: "
                f"{', '.join(extras)}",
            )
        content = item.get("content")
        if fail := _validate_content(content):
            return [], fail
        out.append({"content": content})
    return out, None


def _reject_retired(arguments: dict) -> ToolOutcome | None:
    for key, (reason_code, message) in _RETIRED_KEYS.items():
        if key in arguments:
            return _invalid(reason_code, f"reply.{key} is retired: {message}")
    return None


def _reject_unknown(arguments: dict) -> ToolOutcome | None:
    known = {"action", "analysis", "hold_seconds", "messages", "reply_task_id"}
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
    发言，真发出去了只体现在后续 runtime.reply_flushed 折出的 <my-reply>。"""
    return {
        "reply_task_id": payload.get("reply_task_id"),
        "revision": payload.get("revision"),
        "state": payload.get("state", "open"),
        "flush_at": payload.get("flush_at"),
        "hard_deadline": payload.get("hard_deadline"),
    }

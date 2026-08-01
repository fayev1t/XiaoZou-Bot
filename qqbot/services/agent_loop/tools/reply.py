"""reply 工具：发言两步中的第一步——纯粹地起一段等待，不承载任何内容。

**语义**：调用它表示"我要开口了，但字还没发出去"。等待窗口同时覆盖两件事：
我把这些字打出来本来就需要时间；而这段时间里对方可能还在往下说。所以到点那
一刻的时间线才是落笔依据——这正是把开口拆成两步的全部理由。

**2026-08-01 删除 ``analysis``**：本工具从此**没有任何内容参数**，普通分支
只剩一个 ``hold_seconds``。调用这个动作本身就是意图，不需要第二个字段再承载
一遍。

analysis（更早叫 brief）原本是**双模型交接**的产物——Replyer 要有一份判读才
能组稿。2026-07-31 Replyer 删除后它就没有第二个读者了，降级成"Planner 写给
Planner 自己的备忘"，而 Planner 每一拍本来就要重读整条时间线。留着它有三个
实际害处：
  - **同一段话在时间线上渲染两次以上**：reply 的成功行不折叠（projection
    的 <args> 里一份），到点 ``<reply-task-completed>`` 里又一份，续期 N 次
    就是 N+1 份。这与 2026-07-31 删除派生 ``<my-reply>`` 行的理由是同一条
    ——同样的话渲染两次是复读诱导。
  - **陈旧判读恰好在最坏的时刻被送回**：analysis 写在 T，落笔在 T+hold，而
    这段窗口按设计就是"对方可能还在说"的窗口。它最可能过期的时刻，正好是它
    以"你自己的判断"这种高可信度姿态摆回模型面前的时刻。
  - **与两步走自相矛盾**：要求在"决定要不要等"的那一刻就把局势想透，等于把
    第二步的判断提前钉死在第一步。

砍掉的历史形状依次是：`targets[] {message_id, sender_qq, context, guidance}`
+ `gist {situation, intent, facts, avoid, tone}` 九槽位（2026-07-28，槽位诱导
填充、与 append-only 语义打架）→ 单个自由文本 `brief` → `analysis` → 无。
被回应消息的 message_id 从来不单列字段：落笔那一拍从同一份 timeline 复制真实
id。

**`action` 保留，但改成可选**：省略 = 追加一条授权（普通发言的唯一形态），
`"cancel"` 撤稿。取消的是 `"upsert"` 这个取值——2026-07-24 改 append-only 之后
它就名不副实了（那之前它真是 upsert：带 reply_task_id + expected_revision 做
CAS 合并），留着只是让每次正常发言都先声明一遍状态机操作，把一个纯粹的表达
行为包装成状态迁移。

**`action="verbatim"` 已于 2026-07-30 删除**：当时的理由是双模型分工不该留
后门。2026-07-31 删除 Replyer 后"绕过谁"已无从谈起，但**本工具仍然不承载
最终字句**——它的产出只是一段等待，可见文字只经 ``send_messages`` 发出；
`messages` / `verbatim_messages` 等旧载荷字段继续按迁移 reason_code 拒绝，
不静默降级。

追加仍是 **append-only**（2026-07-24，待办#19）：每次调用都是完整、自足的一
条，不引用上一次。scope 内仍只有一份 open reply_task；后续调用 append 新
revision，并让最新 revision 的 flush_at 直接获胜（能延长也能缩短）。删掉
analysis 之后 latest-revision-wins 只作用于"等到什么时候"这一件事，反而更好
讲。旧 revision 留在事件流供审计与回看——**一串 ``<tool-call name="reply">``
行本身就是可见的自我约束信号**：模型看得见自己已经续了几次。
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
# 内容通道整条删除（2026-08-01，见模块 docstring）。analysis / brief /
# targets / gist / points 是同一条通道先后几代的形状，共用一个 reason_code：
# 它们的问题不是字段名写错了，而是这个工具已经不收任何内容。
_CONTENT_GONE = (
    "content_removed",
    "reply carries no content at all — it only starts the wait. Pass just "
    "hold_seconds; when <reply-task-completed> wakes you, read the timeline "
    "as it stands then and choose the wording with the send_messages tool",
)
# 逐字直发（action="verbatim" + messages）2026-07-30 整条删除，见模块 docstring。
_VERBATIM_GONE = (
    "verbatim_removed",
    "this tool never carries final message text: start the wait, and when "
    "<reply-task-completed> returns, choose the wording and send it with the "
    "send_messages tool",
)
_RETIRED_KEYS: dict[str, tuple[str, str]] = {
    "analysis": _CONTENT_GONE,
    "brief": _CONTENT_GONE,
    "targets": _CONTENT_GONE,
    "gist": _CONTENT_GONE,
    "points": _CONTENT_GONE,
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
        "为当前 scope 起一段短时等待，表示正在输入、字尚未发出，不发送消息，也"
        "不保存任何内容。普通分支只接收 hold_seconds；每次调用追加一条修订，"
        "最新修订的等待时长完整替换旧值。等待结束后，系统写入 "
        "<reply-task-completed> 并唤醒对应 scope，由那一拍结合最新时间线决定说"
        "什么、还说不说。action=cancel 用于撤销当前等待。"
    )
    usage_prompt = _USAGE_PROMPT
    arguments_schema = {
        "type": "object",
        "properties": {
            "hold_seconds": {
                "type": "integer",
                "minimum": 0,
                "maximum": MAX_HOLD_SECONDS,
                "description": (
                    "普通分支必填，表示等待结束前的秒数，无默认值。该时长同时覆盖"
                    "两件事：把这些字打出来本来就需要时间，以及这段时间里对方可能"
                    "继续发言。最新修订中的值完整替换旧值，因此可以缩短或延长等待。"
                ),
            },
            "action": {
                "type": "string",
                "enum": ["cancel"],
                "description": (
                    "省略时进入普通等待分支；取值 cancel 时撤销当前等待。"
                ),
            },
            "reply_task_id": {
                "type": "string",
                "description": (
                    "仅用于 action=cancel，且为可选。提供时必须与当前等待的 "
                    "reply_task_id 一致；省略时撤销当前 scope 中的等待。"
                ),
            },
        },
        "required": [],
        "additionalProperties": False,
        "allOf": [
            {
                "if": {"not": {"required": ["action"]}},
                "then": {
                    "required": ["hold_seconds"],
                    "not": {"required": ["reply_task_id"]},
                },
            },
            {
                "if": {
                    "properties": {"action": {"const": "cancel"}},
                    "required": ["action"],
                },
                "then": {"not": {"required": ["hold_seconds"]}},
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

        hold, fail = _validate_append(arguments)
        if fail:
            return fail

        # ─── append-only（2026-07-24，待办#19）───
        # 每次调用都是完整、自足的一条，不引用上一次：模型不再抄
        # reply_task_id/expected_revision。scope 内只有一份 open reply_task
        # 承载"等到什么时候"，后续调用 append 上去、把 flush_at 换成自己的
        # ——**最新一次调用直接获胜，能延长也能缩短**。删掉 analysis 之后
        # （2026-08-01）这条任务只剩调度事实，latest-revision-wins 也只关乎
        # 时机一件事。旧 revision 仍以 <tool-call name="reply"> 留在 timeline
        # 供回看。
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


def _validate_append(arguments: dict) -> tuple[int, ToolOutcome | None]:
    """append 路径的参数校验：只有一个 hold_seconds，没有别的形态。

    内容参数 2026-08-01 整条删除（analysis/brief/targets/gist/points 走
    _RETIRED_KEYS 拒绝）；逐字直发 2026-07-30 删除（`messages` 同样在那里）。
    """
    # hold_seconds 无默认值（2026-07-24，待办#19）：等多久是每次调用都要现场
    # 判断的语义（我打这几个字要多久、这个人说完了没），给默认值等于替模型做
    # 决定——曾经的 default 0 就把等待窗口整个关掉了。
    if "hold_seconds" not in arguments:
        return (
            0,
            _invalid(
                "missing_hold_seconds",
                "hold_seconds is required; there is no default",
            ),
        )
    hold = _coerce_hold(arguments.get("hold_seconds"))
    if hold is None:
        return 0, _bad_hold()
    return hold, None


def _reject_retired(arguments: dict) -> ToolOutcome | None:
    for key, (reason_code, message) in _RETIRED_KEYS.items():
        if key in arguments:
            return _invalid(reason_code, f"reply.{key} is retired: {message}")
    return None


def _reject_unknown(arguments: dict) -> ToolOutcome | None:
    known = {"action", "hold_seconds", "reply_task_id"}
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
    """成功结果 = 这次等待的身份与调度事实。**永远没有 message_id**——起一段
    等待不是发言；到点后它折成 <reply-task-completed>，真发出去了只体现在后续
    send_messages 调用行的逐条回执上。"""
    return {
        "reply_task_id": payload.get("reply_task_id"),
        "revision": payload.get("revision"),
        "state": payload.get("state", "open"),
        "flush_at": payload.get("flush_at"),
        "hard_deadline": payload.get("hard_deadline"),
    }

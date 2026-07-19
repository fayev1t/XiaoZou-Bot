"""WaitTool —— 模型给自己安排一次延迟唤醒（时间自主权）。

设计动机（2026-07-02，模型+prompt 优先哲学）：此前所有唤醒都是程序驱动的
（外部事件 / 批次收口），模型无法表达"我 10 分钟后再来看看"或"等这个人把话
说完再回"。本工具把"何时思考"的部分决定权交还模型。

职责收窄（2026-07-19，ReplyTask 换轨）："等他把话说完再回"的回复防抖已整体
移交 reply 工具的维持窗口（合稿即顺延，见 group_chat_rules §分条消息并入
reply_task）；wait 只保留自我提醒 / 延迟执行其它动作的用途，description 与
wait.md 不得再引导用它等分条消息。

执行语义（**绝不在工具内 sleep**——ToolWorker 串行跑工具，长眠会卡死整个
派发通道）：execute() 只登记一个 asyncio 定时器就立刻返回成功（带 wake_at）。
到点后回调先写 ``runtime.wait_elapsed``（agent_visible，携带模型当时留下的
note），**再**唤醒 scope——保证醒来那拍的投影必能看到这条 hint（与批次收口
"先写标记再唤醒"同序）。

Best-effort：定时器只活在进程内存里，**进程重启即丢**。可见证据链留给模型
自判：timeline 里有 wait 的 tool-call（result 带 wake_at）但迟迟没有对应的
``<system-hint kind="wait_elapsed">``，说明计时器已丢，可以再约一次。不建
持久化调度表——真到点了没醒，下一条外部消息也会把 loop 叫起来，模型看得到
自己的 wait 记录。

依赖注入：session_factory / wake_scope / tool_call_event_id / correlation_id
全部来自 ToolWorker 统一注入的 run() context，无构造依赖（黑盒不变）。
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any, Awaitable, Callable

from qqbot.core.logging import get_logger
from qqbot.core.time import china_now
from qqbot.services.agent_loop.event_writer import write_runtime_event
from qqbot.services.agent_loop.prompts import load_sibling_md
from qqbot.services.agent_loop.tool_registry import BaseTool, ToolOutcome

logger = get_logger(__name__)

_USAGE_PROMPT = load_sibling_md(__file__, "wait.md")

MIN_WAIT_SECONDS = 5
MAX_WAIT_SECONDS = 3600


class WaitTool(BaseTool):
    """实现 Tool 协议。required_permission 用 BaseTool 默认 GUEST——这是模型
    的自我调度动作，不涉及对任何用户/群的操作，无需触发者授权。"""

    name = "wait"
    description = (
        "Schedule a wake-up for yourself after N seconds. Use this when the "
        "right move is to check back later instead of acting now — e.g. you "
        "promised to follow up in a few minutes, or a running task deserves "
        "a later look. NOT for holding a reply while someone finishes a "
        "multi-part message — that is the reply tool's hold window (merge "
        "the reply_task to postpone its flush). When the timer fires you "
        "get a new tick whose timeline carries "
        '<system-hint kind="wait_elapsed"> echoing your note. The timer is '
        "in-memory: a process restart drops it (your wait tool-call stays "
        "visible in the timeline, so you can tell and re-schedule)."
    )
    usage_prompt = _USAGE_PROMPT
    arguments_schema = {
        "type": "object",
        "properties": {
            "seconds": {
                "type": "integer",
                "minimum": MIN_WAIT_SECONDS,
                "maximum": MAX_WAIT_SECONDS,
                "description": (
                    "Delay before the wake-up, in seconds "
                    f"({MIN_WAIT_SECONDS}-{MAX_WAIT_SECONDS})."
                ),
            },
            "note": {
                "type": "string",
                "description": (
                    "Why you are waiting / what to do on wake-up. Echoed "
                    "back verbatim inside the wait_elapsed hint — this is "
                    "your memo to your future self."
                ),
            },
        },
        "required": ["seconds"],
    }

    async def execute(self, arguments: dict, **context: Any) -> Any:
        seconds = _coerce_seconds(arguments.get("seconds"))
        if seconds is None:
            return ToolOutcome.failure(
                "invalid_arguments",
                "seconds must be an integer",
                reason_code="seconds_not_int",
            )
        if not (MIN_WAIT_SECONDS <= seconds <= MAX_WAIT_SECONDS):
            return ToolOutcome.failure(
                "invalid_arguments",
                (
                    f"seconds must be within [{MIN_WAIT_SECONDS}, "
                    f"{MAX_WAIT_SECONDS}], got {seconds}"
                ),
                reason_code="seconds_out_of_range",
            )
        note_raw = arguments.get("note")
        if note_raw is not None and not isinstance(note_raw, str):
            return ToolOutcome.failure(
                "invalid_arguments",
                "note must be a string",
                reason_code="note_not_str",
            )
        note = (note_raw or "").strip()[:500] or None

        scope_key = context.get("scope_key")
        session_factory = context.get("session_factory")
        wake_scope = context.get("wake_scope")
        if not scope_key or session_factory is None or wake_scope is None:
            # supervisor / session 未注入（早期骨架、残缺测试装配）——工具
            # 黑盒地返回失败，不 raise、不静默假装约上了。
            return ToolOutcome.failure(
                "internal_tool_error",
                "wait unavailable: missing wake_scope/session context",
            )

        wake_at = china_now() + timedelta(seconds=seconds)
        loop = asyncio.get_running_loop()
        loop.call_later(
            seconds,
            lambda: asyncio.ensure_future(
                _fire_wait(
                    session_factory=session_factory,
                    wake_scope=wake_scope,
                    scope_key=scope_key,
                    correlation_id=context.get("correlation_id"),
                    causation_id=context.get("tool_call_event_id"),
                    seconds=seconds,
                    note=note,
                    wake_at_iso=wake_at.isoformat(timespec="seconds"),
                )
            ),
        )
        result: dict[str, Any] = {
            "scheduled": True,
            "seconds": seconds,
            "wake_at": wake_at.isoformat(timespec="seconds"),
        }
        if note:
            result["note"] = note
        return ToolOutcome.success(result)


async def _fire_wait(
    *,
    session_factory: Any,
    wake_scope: Callable[[str], Awaitable[None]],
    scope_key: str,
    correlation_id: str | None,
    causation_id: str | None,
    seconds: int,
    note: str | None,
    wake_at_iso: str,
) -> None:
    """定时器到点回调：先写 runtime.wait_elapsed 再唤醒 scope。

    两步各自兜异常：事件写失败仍要唤醒（宁可模型醒来少一条 hint，不可失约）；
    唤醒失败只记日志（loop 已停 / 进程收尾中）。causation 指向当初的
    agent.tool_called，correlation 沿用发起 tick 的——与 tool_result 的因果
    语义一致。
    """
    payload: dict[str, Any] = {"seconds": seconds, "wake_at": wake_at_iso}
    if note:
        payload["note"] = note
    try:
        await write_runtime_event(
            session_factory,
            event_type="runtime.wait_elapsed",
            scope_key=scope_key,
            visibility="agent_visible",
            correlation_id=correlation_id,
            causation_id=causation_id,
            payload=payload,
        )
    except Exception as exc:
        logger.warning(
            "[wait] write wait_elapsed failed (still waking {}): {}",
            scope_key,
            exc,
        )
    try:
        await wake_scope(scope_key)
    except Exception as exc:
        logger.warning("[wait] wake {} failed: {}", scope_key, exc)


def _coerce_seconds(raw: Any) -> int | None:
    """LLM 给的 seconds → int；bool / 非整数 / 非法字符串 → None。"""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        s = raw.strip()
        if s.isdigit():
            return int(s)
    return None

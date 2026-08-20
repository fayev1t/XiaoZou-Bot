"""静默门：登记完成之后才判定叫不叫醒。注册层不打 tag。

runtime_only → 不叫、不 note_activity
runtime.silence_elapsed → 叫、不 note_activity
其它 agent_visible → 叫 + note_activity
private 不实例化 loop，也不叫
空程序停止符在 AgentLoop 里，不在这里。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

SILENCE_ELAPSED_TYPE = "runtime.silence_elapsed"

WakeFn = Callable[[str], Awaitable[None]]
ActivityFn = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class SilenceGateDecision:
    wake: bool
    note_activity: bool
    scope_key: str | None


def scope_key_of(event: Any) -> str | None:
    scope = getattr(event, "scope", None)
    if scope == "group" and getattr(event, "group_id", None) is not None:
        return f"group:{event.group_id}"
    if scope == "system":
        return "system"
    if scope == "private" and getattr(event, "user_id", None) is not None:
        return f"private:{event.user_id}"
    return None


def decide_silence_gate(
    *,
    event_type: str,
    visibility: str,
    scope_key: str | None,
) -> SilenceGateDecision:
    if not scope_key or scope_key.startswith("private:"):
        return SilenceGateDecision(False, False, scope_key)
    if visibility != "agent_visible":
        return SilenceGateDecision(False, False, scope_key)
    if event_type == SILENCE_ELAPSED_TYPE:
        return SilenceGateDecision(True, False, scope_key)
    return SilenceGateDecision(True, True, scope_key)


def decide_silence_gate_for_event(event: Any) -> SilenceGateDecision:
    return decide_silence_gate(
        event_type=str(getattr(event, "type", "") or ""),
        visibility=str(getattr(event, "visibility", "") or ""),
        scope_key=scope_key_of(event),
    )


async def apply_silence_gate(
    event: Any,
    *,
    wake: WakeFn | None,
    note_activity: ActivityFn | None,
) -> SilenceGateDecision:
    decision = decide_silence_gate_for_event(event)
    if (
        decision.note_activity
        and note_activity is not None
        and decision.scope_key is not None
    ):
        try:
            note_activity(decision.scope_key)
        except Exception as exc:
            logger.warning(
                "[silence_gate] note_activity %s failed: %s",
                decision.scope_key,
                exc,
            )
    if decision.wake and wake is not None and decision.scope_key is not None:
        try:
            await wake(decision.scope_key)
        except Exception as exc:
            logger.warning(
                "[silence_gate] wake %s failed: %s",
                decision.scope_key,
                exc,
            )
    return decision

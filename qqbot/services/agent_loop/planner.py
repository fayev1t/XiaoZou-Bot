"""Skeleton planner implementations.

FakeIdlePlanner is the bootstrap planner: it always returns IdleAction.
Its job is to verify the loop wiring (events → tick → events) end-to-end
before a real LLM planner is plugged in.
"""

from __future__ import annotations

from qqbot.services.agent_loop.decision import (
    DecisionContext,
    DecisionOutput,
    IdleAction,
)


class FakeIdlePlanner:
    async def decide(self, context: DecisionContext) -> DecisionOutput:
        _ = context
        return DecisionOutput(
            actions=[IdleAction(reason="bootstrap_skeleton")],
            reasoning="v2 loop skeleton: real LLM planner not yet wired",
        )

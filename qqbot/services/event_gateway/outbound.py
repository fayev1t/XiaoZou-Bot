"""出口网关：模型/工具响应绕回入口网关当上游事件。

超时等错误处理在模型提供层做完，再把失败包送回来登记。
"""

from __future__ import annotations

import asyncio
from typing import Any

from qqbot.core.logging import get_logger

logger = get_logger(__name__)

_inbound: Any = None


def set_inbound_gateway(gateway: Any) -> None:
    global _inbound
    _inbound = gateway


def get_inbound_gateway() -> Any:
    return _inbound


async def submit_model_outcome(payload: dict[str, Any]) -> None:
    gateway = _inbound
    if gateway is None:
        return
    try:
        await gateway.submit("model", payload, source=payload)
    except Exception as exc:
        logger.warning("[event_gateway] model outcome submit failed: {}", exc)


async def submit_tool_outcome(payload: dict[str, Any]) -> None:
    gateway = _inbound
    if gateway is None:
        return
    try:
        await gateway.submit("tool", payload, source=payload)
    except Exception as exc:
        logger.warning("[event_gateway] tool outcome submit failed: {}", exc)


def schedule_model_outcome(payload: dict[str, Any]) -> None:
    """模型提供层用：不阻塞 ainvoke 返回。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(submit_model_outcome(payload))

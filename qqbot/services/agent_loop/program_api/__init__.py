"""NapCat / OneBot 传输网关与尚未暴露给 Planner 的 Raw 程序原语。

``OneBotGateway`` 是现役 Program Tool 共用的请求—响应边界；具名 Raw 函数仍不
注册为模型函数。导入本包本身不会改变 ToolRegistry 或 Planner prompt。
"""

from qqbot.services.agent_loop.program_api.onebot_gateway import (
    OneBotGateway,
    RawOneBotResponse,
    RawOneBotResult,
    RawTransportFailure,
)
from qqbot.services.agent_loop.program_api.raw_onebot import RawOneBotProgramFunctions

__all__ = [
    "OneBotGateway",
    "RawOneBotProgramFunctions",
    "RawOneBotResponse",
    "RawOneBotResult",
    "RawTransportFailure",
]

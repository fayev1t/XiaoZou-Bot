"""尚未接入 Planner 的程序形态 API 原语。

本包与现役 ``tools`` 路线并行存在。导入本包不会注册任何工具，也不会改变
AgentLoop、提示词或现有 Tool 的调用路径。
"""

from qqbot.services.agent_loop.program_api.raw_onebot import (
    RawOneBotProgramFunctions,
    RawOneBotResponse,
    RawOneBotResult,
    RawTransportFailure,
)

__all__ = [
    "RawOneBotProgramFunctions",
    "RawOneBotResponse",
    "RawOneBotResult",
    "RawTransportFailure",
]

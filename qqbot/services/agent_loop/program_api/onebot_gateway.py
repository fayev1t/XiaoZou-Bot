"""统一的 NapCat / OneBot 请求—响应边界。

现役 Program Tool 与预留的 Raw OneBot 同名函数共用本模块。调用侧只声明本次
请求是 Query 还是 Effect；网关负责取得 Bot、选择同名方法或 ``call_api``、保留
响应 envelope，并把没有响应的传输故障规范化为 ``RawTransportFailure``。

这里不做 scope 注入、权限判断、参数裁剪、结果 DTO 化、业务补偿或自动重试。
Effect 一旦可能已经出手，超时或断连只标记 ``uncertain``，绝不据此重放。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, TypeAlias

from qqbot.services.agent_loop import bot_registry

BotProvider: TypeAlias = Callable[[], Any | None]


@dataclass(frozen=True, slots=True)
class RawOneBotResponse:
    """NapCat 已给出响应，或 NoneBot 成功解包后重建的响应 envelope。"""

    action: str
    status: str | None
    retcode: int | str | None
    data: Any
    message: str | None = None
    wording: str | None = None
    stream: str | None = None
    echo: Any = None

    @property
    def ok(self) -> bool:
        """响应是否明确表示 action 成功。"""
        return self.status == "ok" and self.retcode in (0, "0")

    def as_dict(self) -> dict[str, Any]:
        """返回可继续脱敏、持久化的普通 response envelope。"""
        return {
            "action": self.action,
            "status": self.status,
            "retcode": self.retcode,
            "data": self.data,
            "message": self.message,
            "wording": self.wording,
            "stream": self.stream,
            "echo": self.echo,
        }


@dataclass(frozen=True, slots=True)
class RawTransportFailure:
    """调用没有拿到 OneBot 响应；不是 NapCat 明确返回的业务失败。"""

    action: str
    error_kind: str
    message: str
    uncertain: bool

    @property
    def status(self) -> str:
        """映射到 terminal receipt 使用的确定性状态。"""
        return "uncertain" if self.uncertain else "failed"


RawOneBotResult: TypeAlias = RawOneBotResponse | RawTransportFailure

_TIMEOUT_EXCEPTION_NAMES = frozenset(
    {
        "ConnectTimeout",
        "PoolTimeout",
        "ReadTimeout",
        "RequestTimeout",
        "WriteTimeout",
    }
)
_NETWORK_EXCEPTION_NAMES = frozenset(
    {
        "BrokenResourceError",
        "ConnectError",
        "ConnectionClosed",
        "EndOfStream",
        "NetworkError",
        "ReadError",
        "RemoteProtocolError",
        "WriteError",
    }
)
_TRANSPORT_MODULE_ROOTS = frozenset({"anyio", "httpcore", "httpx", "websockets"})


class OneBotGateway:
    """Program-facing OneBot 调用的唯一传输网关。"""

    def __init__(self, bot_provider: BotProvider | None = None) -> None:
        self._bot_provider = (
            bot_provider if bot_provider is not None else bot_registry.get_any
        )

    async def query(self, action: str, **params: Any) -> RawOneBotResult:
        """执行只读 action；无响应是确定的查询失败，不代表外部副作用。"""
        return await self._call(action, effect=False, params=params)

    async def effect(self, action: str, **params: Any) -> RawOneBotResult:
        """执行副作用 action；无响应保守标记为可能已经执行。"""
        return await self._call(action, effect=True, params=params)

    async def _call(
        self,
        action: str,
        *,
        effect: bool,
        params: dict[str, Any],
    ) -> RawOneBotResult:
        bot = self._bot_provider()
        if bot is None:
            return RawTransportFailure(
                action=action,
                error_kind="no_bot_available",
                message="no bot available",
                uncertain=False,
            )

        method = getattr(bot, action, None)
        try:
            if callable(method):
                raw = await method(**params)
            else:
                raw = await bot.call_api(action, **params)
        except Exception as exc:
            response = _response_from_exception(action, exc)
            if response is not None:
                return response
            error_kind = _transport_error_kind(exc)
            if error_kind is None:
                raise
            return RawTransportFailure(
                action=action,
                error_kind=error_kind,
                message=_exception_message(exc),
                uncertain=effect,
            )
        return _response_from_result(action, raw)


def _response_from_result(action: str, raw: Any) -> RawOneBotResponse:
    if _is_response_envelope(raw):
        return _response_from_envelope(action, raw)
    # NoneBot 的 Bot 方法在成功时通常只返回 BaseResponse.data；只重建能够确定
    # 的字段，不伪造 stream 或 echo。
    return RawOneBotResponse(
        action=action,
        status="ok",
        retcode=0,
        data=raw,
    )


def _response_from_exception(action: str, exc: Exception) -> RawOneBotResponse | None:
    info = getattr(exc, "info", None)
    if not _is_response_envelope(info):
        return None
    return _response_from_envelope(action, info)


def _is_response_envelope(value: Any) -> bool:
    return isinstance(value, Mapping) and "status" in value and "retcode" in value


def _response_from_envelope(
    action: str, envelope: Mapping[str, Any]
) -> RawOneBotResponse:
    return RawOneBotResponse(
        action=action,
        status=envelope.get("status"),
        retcode=envelope.get("retcode"),
        data=envelope.get("data"),
        message=envelope.get("message"),
        wording=envelope.get("wording"),
        stream=envelope.get("stream"),
        echo=envelope.get("echo"),
    )


def _transport_error_kind(exc: Exception) -> str | None:
    exc_type = type(exc)
    if isinstance(exc, TimeoutError) or exc_type.__name__ in _TIMEOUT_EXCEPTION_NAMES:
        return "timeout"
    if isinstance(exc, ConnectionError):
        return "network_error"
    if exc_type.__name__ in _NETWORK_EXCEPTION_NAMES:
        return "network_error"
    module_root = exc_type.__module__.partition(".")[0]
    if module_root in _TRANSPORT_MODULE_ROOTS:
        return "network_error"
    return None


def _exception_message(exc: Exception) -> str:
    detail = str(exc)
    return f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__


__all__ = [
    "BotProvider",
    "OneBotGateway",
    "RawOneBotResponse",
    "RawOneBotResult",
    "RawTransportFailure",
]

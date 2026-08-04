"""现役 NapCat 能力所需的 Raw OneBot 程序函数。

这些函数只负责三件事：把具名参数组成 OneBot JSON、调用同名 action、保留或
重建响应 envelope。它们刻意不复用现役 Tool，也不做 scope 注入、权限检查、
参数裁剪、请求 flag 反查、消息段处理或结果 DTO 化。

当前模块只提供函数实现，尚未进入任何 registry、Planner prompt 或 AgentLoop。
副作用 action 不自动重试；拿不到响应时只返回 ``RawTransportFailure``，由未来的
程序执行器负责落 effect terminal。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, TypeAlias

from qqbot.services.agent_loop import bot_registry

BotProvider: TypeAlias = Callable[[], Any | None]
OneBotId: TypeAlias = int | str


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


@dataclass(frozen=True, slots=True)
class RawTransportFailure:
    """调用没有拿到 OneBot 响应；不是 NapCat 明确返回的业务失败。"""

    action: str
    error_kind: str
    message: str
    uncertain: bool


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


class RawOneBotProgramFunctions:
    """第一批 8 个 Raw action 的绑定对象。

    ``bot_provider`` 默认读取现有 bot registry，但可以在测试或未来执行器中注入。
    每个公开方法都与 OneBot action 同名；不存在同名 Bot 方法时回退
    ``bot.call_api(action, **params)``。
    """

    def __init__(self, bot_provider: BotProvider = bot_registry.get_any) -> None:
        self._bot_provider = bot_provider

    async def send_group_msg(  # noqa: PLR0913 - Raw action 保留完整具名参数面
        self,
        *,
        message: Any,
        message_type: Literal["private", "group"] | None = None,
        user_id: OneBotId | None = None,
        group_id: OneBotId | None = None,
        auto_escape: bool | str | None = None,
        source: str | None = None,
        news: list[dict[str, Any]] | None = None,
        summary: str | None = None,
        prompt: str | None = None,
        timeout: float | None = None,
    ) -> RawOneBotResult:
        """调用 NapCat ``send_group_msg``，保留其扩展参数面。"""
        return await self._call(
            "send_group_msg",
            effect=True,
            params=_request_params(
                required={"message": message},
                optional={
                    "message_type": message_type,
                    "user_id": user_id,
                    "group_id": group_id,
                    "auto_escape": auto_escape,
                    "source": source,
                    "news": news,
                    "summary": summary,
                    "prompt": prompt,
                    "timeout": timeout,
                },
            ),
        )

    async def get_group_info(
        self,
        *,
        group_id: OneBotId,
        no_cache: bool | str | None = None,
    ) -> RawOneBotResult:
        """调用 ``get_group_info``。

        NapCat 4.18.13 OpenAPI 未声明 ``no_cache``，但 OneBot 兼容实现和现役
        Tool 会使用它，因此作为不注入默认值的兼容可选字段保留。
        """
        return await self._call(
            "get_group_info",
            effect=False,
            params=_request_params(
                required={"group_id": group_id},
                optional={"no_cache": no_cache},
            ),
        )

    async def get_group_member_list(
        self,
        *,
        group_id: OneBotId,
        no_cache: bool | str | None = None,
    ) -> RawOneBotResult:
        """调用 ``get_group_member_list``。"""
        return await self._call(
            "get_group_member_list",
            effect=False,
            params=_request_params(
                required={"group_id": group_id},
                optional={"no_cache": no_cache},
            ),
        )

    async def get_group_member_info(
        self,
        *,
        group_id: OneBotId,
        user_id: OneBotId,
        no_cache: bool | str | None = None,
    ) -> RawOneBotResult:
        """调用 ``get_group_member_info``。"""
        return await self._call(
            "get_group_member_info",
            effect=False,
            params=_request_params(
                required={"group_id": group_id, "user_id": user_id},
                optional={"no_cache": no_cache},
            ),
        )

    async def get_group_system_msg(
        self,
        *,
        count: float | str | None = None,
    ) -> RawOneBotResult:
        """调用 ``get_group_system_msg``。

        OpenAPI 把 ``count`` 标成必填，但现役 NapCat 调用允许省略；这里用 ``None``
        表示不发送该字段，让上游沿用自己的默认行为。
        """
        return await self._call(
            "get_group_system_msg",
            effect=False,
            params=_request_params(required={}, optional={"count": count}),
        )

    async def set_group_add_request(
        self,
        *,
        flag: str,
        sub_type: str | None = None,
        approve: bool | str | None = None,
        reason: str | None = None,
        count: float | None = None,
    ) -> RawOneBotResult:
        """调用 ``set_group_add_request``。

        ``sub_type`` 虽未出现在 NapCat 4.18.13 的 properties 中，却出现在其示例，
        也是 OneBot 标准和现役 Tool 的实际参数，因此作为兼容字段保留。
        """
        return await self._call(
            "set_group_add_request",
            effect=True,
            params=_request_params(
                required={"flag": flag},
                optional={
                    "sub_type": sub_type,
                    "approve": approve,
                    "reason": reason,
                    "count": count,
                },
            ),
        )

    async def set_group_kick(
        self,
        *,
        group_id: OneBotId,
        user_id: OneBotId,
        reject_add_request: bool | str | None = None,
    ) -> RawOneBotResult:
        """调用 ``set_group_kick``，不做角色层级或自踢检查。"""
        return await self._call(
            "set_group_kick",
            effect=True,
            params=_request_params(
                required={"group_id": group_id, "user_id": user_id},
                optional={"reject_add_request": reject_add_request},
            ),
        )

    async def set_group_leave(
        self,
        *,
        group_id: OneBotId,
        is_dismiss: bool | str | None = None,
    ) -> RawOneBotResult:
        """调用 ``set_group_leave``，不替调用者固定 ``is_dismiss``。"""
        return await self._call(
            "set_group_leave",
            effect=True,
            params=_request_params(
                required={"group_id": group_id},
                optional={"is_dismiss": is_dismiss},
            ),
        )

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


def _request_params(
    *, required: Mapping[str, Any], optional: Mapping[str, Any]
) -> dict[str, Any]:
    """保留所有必填值；可选值为 ``None`` 时不写进 OneBot JSON。"""
    return {
        **required,
        **{key: value for key, value in optional.items() if value is not None},
    }


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
    "RawOneBotProgramFunctions",
    "RawOneBotResponse",
    "RawOneBotResult",
    "RawTransportFailure",
]

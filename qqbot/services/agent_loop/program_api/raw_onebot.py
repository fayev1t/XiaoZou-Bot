"""现役 NapCat 能力所需的具名 Raw OneBot 程序函数。

这些函数只负责把具名参数组成 OneBot JSON，再交给现役 ``OneBotGateway``。
它们刻意不复用 Tool，也不做 scope 注入、权限检查、参数裁剪、请求 flag 反查、
消息段处理或结果 DTO 化。

这些 Raw 函数仍未进入 ToolRegistry 或 Planner prompt；变化只在于它们与现役 Tool
共用同一个传输网关，不再维护第二套响应和异常分类逻辑。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, TypeAlias

from qqbot.services.agent_loop.program_api.onebot_gateway import (
    BotProvider,
    OneBotGateway,
    RawOneBotResponse,
    RawOneBotResult,
    RawTransportFailure,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

OneBotId: TypeAlias = int | str


class RawOneBotProgramFunctions:
    """第一批 8 个 Raw action 的绑定对象。

    ``bot_provider`` 默认读取现有 bot registry，但可以在测试或未来执行器中注入。
    每个公开方法都与 OneBot action 同名；不存在同名 Bot 方法时回退
    ``bot.call_api(action, **params)``。
    """

    def __init__(self, bot_provider: BotProvider | None = None) -> None:
        self._gateway = OneBotGateway(bot_provider)

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
        call = self._gateway.effect if effect else self._gateway.query
        return await call(action, **params)


def _request_params(
    *, required: Mapping[str, Any], optional: Mapping[str, Any]
) -> dict[str, Any]:
    """保留所有必填值；可选值为 ``None`` 时不写进 OneBot JSON。"""
    return {
        **required,
        **{key: value for key, value in optional.items() if value is not None},
    }


__all__ = [
    "RawOneBotProgramFunctions",
    "RawOneBotResponse",
    "RawOneBotResult",
    "RawTransportFailure",
]

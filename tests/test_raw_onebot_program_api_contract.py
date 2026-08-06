"""OneBotGateway 与 Raw OneBot 程序函数的隔离、响应语义契约。

现役 Tool 和这批具名 Raw 函数共用网关，但 Raw 函数仍不进入模型注册表。本文件钉住：

- 8 个 action 具名存在，参数不注入、不转换，可选 ``None`` 不发送；
- 优先使用 Bot 同名方法，缺失时回退 ``call_api``；
- NapCat 明确响应（含 failed）保留为 ``RawOneBotResponse``；
- 真正无响应才返回 ``RawTransportFailure``，effect 才可能 ``uncertain``；
- 宿主编程错误继续抛出，且现役 18 个 Tool 注册不发生变化。
"""

from __future__ import annotations

import ast
import inspect
import unittest
from pathlib import Path
from typing import Any

from qqbot.services.agent_loop.program_api import (
    OneBotGateway,
    RawOneBotProgramFunctions,
    RawOneBotResponse,
    RawTransportFailure,
)

_ACTIONS = (
    "send_group_msg",
    "get_group_info",
    "get_group_member_list",
    "get_group_member_info",
    "get_group_system_msg",
    "set_group_add_request",
    "set_group_kick",
    "set_group_leave",
)
_DEFAULT_RESPONSE = object()


class _MethodBot:
    def __init__(
        self,
        *,
        response: Any = _DEFAULT_RESPONSE,
        error: Exception | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.response = response
        self.error = error

    def __getattr__(self, action: str) -> Any:
        if action not in _ACTIONS:
            raise AttributeError(action)

        async def call(**params: Any) -> Any:
            self.calls.append((action, params))
            if self.error is not None:
                raise self.error
            if self.response is _DEFAULT_RESPONSE:
                return {"called_action": action}
            return self.response

        return call


class _CallApiOnlyBot:
    def __init__(self, response: Any) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.response = response

    async def call_api(self, action: str, **params: Any) -> Any:
        self.calls.append((action, params))
        return self.response


class _FakeActionFailedError(Exception):
    def __init__(self, info: dict[str, Any]) -> None:
        super().__init__("NapCat rejected the action")
        self.info = info


class NetworkError(Exception):
    pass


class TimeoutConfigurationError(Exception):
    pass


class RawOneBotProgramApiTests(unittest.IsolatedAsyncioTestCase):
    def test_exact_first_batch_methods_exist(self) -> None:
        for action in _ACTIONS:
            with self.subTest(action=action):
                method = getattr(RawOneBotProgramFunctions, action, None)
                self.assertTrue(inspect.iscoroutinefunction(method))

    async def test_all_raw_parameters_are_forwarded_without_coercion(self) -> None:
        bot = _MethodBot()
        raw = RawOneBotProgramFunctions(bot_provider=lambda: bot)

        await raw.send_group_msg(
            message=[{"type": "text", "data": {"text": "hi"}}],
            message_type="group",
            user_id="20002",
            group_id="10001",
            auto_escape=False,
            source="source",
            news=[],
            summary="",
            prompt="prompt",
            timeout=0,
        )
        await raw.get_group_info(group_id="10001", no_cache=True)
        await raw.get_group_member_list(group_id=10001, no_cache="false")
        await raw.get_group_member_info(
            group_id="10001", user_id="20002", no_cache=False
        )
        await raw.get_group_system_msg(count="50")
        await raw.set_group_add_request(
            flag="flag-1",
            sub_type="add",
            approve=False,
            reason="",
            count=0,
        )
        await raw.set_group_kick(
            group_id="10001",
            user_id=20002,
            reject_add_request=False,
        )
        await raw.set_group_leave(group_id=10001, is_dismiss=True)

        self.assertEqual(
            bot.calls,
            [
                (
                    "send_group_msg",
                    {
                        "message": [{"type": "text", "data": {"text": "hi"}}],
                        "message_type": "group",
                        "user_id": "20002",
                        "group_id": "10001",
                        "auto_escape": False,
                        "source": "source",
                        "news": [],
                        "summary": "",
                        "prompt": "prompt",
                        "timeout": 0,
                    },
                ),
                ("get_group_info", {"group_id": "10001", "no_cache": True}),
                (
                    "get_group_member_list",
                    {"group_id": 10001, "no_cache": "false"},
                ),
                (
                    "get_group_member_info",
                    {
                        "group_id": "10001",
                        "user_id": "20002",
                        "no_cache": False,
                    },
                ),
                ("get_group_system_msg", {"count": "50"}),
                (
                    "set_group_add_request",
                    {
                        "flag": "flag-1",
                        "sub_type": "add",
                        "approve": False,
                        "reason": "",
                        "count": 0,
                    },
                ),
                (
                    "set_group_kick",
                    {
                        "group_id": "10001",
                        "user_id": 20002,
                        "reject_add_request": False,
                    },
                ),
                (
                    "set_group_leave",
                    {"group_id": 10001, "is_dismiss": True},
                ),
            ],
        )

    async def test_optional_none_values_are_not_sent(self) -> None:
        bot = _MethodBot()
        raw = RawOneBotProgramFunctions(bot_provider=lambda: bot)

        await raw.send_group_msg(message="hi")
        await raw.get_group_info(group_id=1)
        await raw.get_group_member_list(group_id=1)
        await raw.get_group_member_info(group_id=1, user_id=2)
        await raw.get_group_system_msg()
        await raw.set_group_add_request(flag="flag")
        await raw.set_group_kick(group_id=1, user_id=2)
        await raw.set_group_leave(group_id=1)

        self.assertEqual(
            bot.calls,
            [
                ("send_group_msg", {"message": "hi"}),
                ("get_group_info", {"group_id": 1}),
                ("get_group_member_list", {"group_id": 1}),
                (
                    "get_group_member_info",
                    {"group_id": 1, "user_id": 2},
                ),
                ("get_group_system_msg", {}),
                ("set_group_add_request", {"flag": "flag"}),
                ("set_group_kick", {"group_id": 1, "user_id": 2}),
                ("set_group_leave", {"group_id": 1}),
            ],
        )

    async def test_success_data_rebuilds_ok_envelope(self) -> None:
        bot = _MethodBot(response={"message_id": 123})
        result = await RawOneBotProgramFunctions(
            bot_provider=lambda: bot
        ).send_group_msg(group_id=1, message="hi")

        self.assertEqual(
            result,
            RawOneBotResponse(
                action="send_group_msg",
                status="ok",
                retcode=0,
                data={"message_id": 123},
            ),
        )
        self.assertTrue(result.ok)

    async def test_complete_envelope_is_preserved(self) -> None:
        envelope = {
            "status": "failed",
            "retcode": 1404,
            "data": None,
            "message": "failed",
            "wording": "群不存在",
            "stream": "normal-action",
            "echo": {"request_id": "E1"},
        }
        bot = _MethodBot(response=envelope)
        result = await RawOneBotProgramFunctions(
            bot_provider=lambda: bot
        ).get_group_info(group_id=1)

        self.assertEqual(
            result,
            RawOneBotResponse(action="get_group_info", **envelope),
        )
        self.assertFalse(result.ok)

    async def test_action_failed_info_returns_raw_response(self) -> None:
        envelope = {
            "status": "failed",
            "retcode": 1404,
            "data": None,
            "message": "failed",
            "wording": "权限不足",
            "stream": "normal-action",
            "echo": "E2",
        }
        bot = _MethodBot(error=_FakeActionFailedError(envelope))
        result = await RawOneBotProgramFunctions(
            bot_provider=lambda: bot
        ).set_group_kick(group_id=1, user_id=2)

        self.assertEqual(
            result,
            RawOneBotResponse(action="set_group_kick", **envelope),
        )

    async def test_query_transport_failure_is_not_uncertain(self) -> None:
        bot = _MethodBot(error=TimeoutError("timed out"))
        result = await RawOneBotProgramFunctions(
            bot_provider=lambda: bot
        ).get_group_info(group_id=1)

        if not isinstance(result, RawTransportFailure):
            self.fail(f"expected RawTransportFailure, got {result!r}")
        self.assertEqual(result.error_kind, "timeout")
        self.assertFalse(result.uncertain)
        self.assertIn("timed out", result.message)

    async def test_effect_transport_failure_is_uncertain(self) -> None:
        bot = _MethodBot(error=NetworkError("connection lost"))
        result = await RawOneBotProgramFunctions(
            bot_provider=lambda: bot
        ).set_group_leave(group_id=1)

        if not isinstance(result, RawTransportFailure):
            self.fail(f"expected RawTransportFailure, got {result!r}")
        self.assertEqual(result.error_kind, "network_error")
        self.assertTrue(result.uncertain)

    async def test_no_bot_is_certain_because_no_call_was_attempted(self) -> None:
        result = await RawOneBotProgramFunctions(
            bot_provider=lambda: None
        ).send_group_msg(group_id=1, message="hi")

        self.assertEqual(
            result,
            RawTransportFailure(
                action="send_group_msg",
                error_kind="no_bot_available",
                message="no bot available",
                uncertain=False,
            ),
        )

    async def test_falls_back_to_call_api_when_named_method_is_missing(self) -> None:
        bot = _CallApiOnlyBot(response=[{"user_id": 2}])
        result = await RawOneBotProgramFunctions(
            bot_provider=lambda: bot
        ).get_group_member_list(group_id="1", no_cache=True)

        self.assertEqual(
            bot.calls,
            [
                (
                    "get_group_member_list",
                    {"group_id": "1", "no_cache": True},
                )
            ],
        )
        if not isinstance(result, RawOneBotResponse):
            self.fail(f"expected RawOneBotResponse, got {result!r}")
        self.assertEqual(result.data, [{"user_id": 2}])

    async def test_host_programming_error_is_not_disguised_as_transport(self) -> None:
        for error in (
            ValueError("bad host code"),
            TimeoutConfigurationError("bad timeout configuration"),
        ):
            with self.subTest(error=error):
                bot = _MethodBot(error=error)
                with self.assertRaises(type(error)):
                    await RawOneBotProgramFunctions(
                        bot_provider=lambda bot=bot: bot
                    ).get_group_info(group_id=1)


class RawOneBotIsolationTests(unittest.TestCase):
    def test_gateway_and_raw_module_do_not_reuse_existing_tools(self) -> None:
        from qqbot.services.agent_loop.program_api import onebot_gateway, raw_onebot

        for module in (onebot_gateway, raw_onebot):
            with self.subTest(module=module.__name__):
                source = inspect.getsource(module)
                self.assertNotIn("agent_loop.tools", source)
                self.assertNotIn("ToolOutcome", source)
                self.assertNotIn("call_action", source)

    def test_gateway_is_exported_but_not_model_registered(self) -> None:
        self.assertTrue(inspect.isclass(OneBotGateway))
        root = Path(__file__).resolve().parents[1]
        registry_path = root / "qqbot/services/agent_loop/tools/__init__.py"
        source = registry_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        builder = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "build_default_registry"
        )
        registered = []
        for node in ast.walk(builder):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            func = node.func
            if not (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "registry"
                and func.attr == "register"
            ):
                continue
            argument = node.args[0]
            if isinstance(argument, ast.Name):
                registered.append(argument.id)
            elif isinstance(argument, ast.Call) and isinstance(
                argument.func, ast.Name
            ):
                registered.append(argument.func.id)

        self.assertEqual(
            registered,
            [
                "TaskTool",
                "ReplyTool",
                "SendMessagesTool",
                "WaitTool",
                "ReflectTool",
                "GetRecentThoughtsTool",
                "RespondToGroupJoinRequestTool",
                "MemeCollectionTool",
                "LookAtImageTool",
                "GetGroupInfoTool",
                "GetMemberListTool",
                "GetMemberInfoTool",
                "GetPendingJoinRequestsTool",
                "KickTool",
                "LeaveGroupTool",
                "WebsearchTool",
                "WebfetchTool",
                "SearchHistoryTool",
            ],
        )
        self.assertNotIn("OneBotGateway", registered)
        self.assertNotIn("RawOneBotProgramFunctions", registered)

    def test_program_tool_paths_do_not_bypass_gateway(self) -> None:
        root = Path(__file__).resolve().parents[1] / "qqbot/services/agent_loop"
        paths = [
            root / "outbound_messages.py",
            root / "tool_registry.py",
            *(root / "tools").glob("*.py"),
        ]
        direct_calls: list[str] = []
        missing_kind: list[str] = []
        for path in paths:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if (
                    isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "bot"
                ):
                    direct_calls.append(f"{path.name}:{node.lineno}:{func.attr}")
                if (
                    isinstance(func, ast.Name)
                    and func.id == "call_action"
                    and not any(keyword.arg == "effect" for keyword in node.keywords)
                ):
                    missing_kind.append(f"{path.name}:{node.lineno}")

        self.assertEqual(direct_calls, [])
        self.assertEqual(missing_kind, [])


if __name__ == "__main__":
    unittest.main()

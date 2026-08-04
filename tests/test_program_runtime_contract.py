"""Runtime, read-only ABI, quota, and failure contracts for Planner programs."""

# Tool stubs intentionally keep registry metadata and call recordings on the class.
# ruff: noqa: ARG002, RUF012, SIM117

from __future__ import annotations

import asyncio
import unittest
from typing import Any
from unittest.mock import AsyncMock, patch

from qqbot.services.agent_loop.program_ast import preflight
from qqbot.services.agent_loop.program_events import EffectCallHandle
from qqbot.services.agent_loop.program_runtime import (
    ProgramExecutionError,
    ProgramExecutor,
)
from qqbot.services.agent_loop.tool_registry import (
    BaseTool,
    ToolGeneratedEvent,
    ToolOutcome,
    ToolRegistry,
)


class _MembersQuery(BaseTool):
    name = "members"
    program_kind = "query"
    arguments_schema = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    result_schema = {
        "type": "object",
        "properties": {
            "members": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "user_id": {"type": "integer"},
                        "nickname": {"type": "string"},
                        "card": {"type": ["string", "null"]},
                    },
                    "additionalProperties": False,
                },
            }
        },
        "additionalProperties": False,
    }
    calls: list[dict] = []

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        type(self).calls.append(dict(arguments))
        return ToolOutcome.success(
            {
                "members": [
                    {"user_id": 1, "nickname": "Alice"},
                    {"user_id": 2, "nickname": "Bob", "card": "B"},
                ]
            }
        )


class _EchoQuery(BaseTool):
    name = "echo"
    program_kind = "query"
    arguments_schema = {
        "type": "object",
        "properties": {"value": {}},
        "required": ["value"],
        "additionalProperties": False,
    }
    result_schema = {
        "type": "object",
        "properties": {"value": {}},
        "additionalProperties": False,
    }
    calls: list[Any] = []

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        type(self).calls.append(arguments["value"])
        return ToolOutcome.success({"value": arguments["value"]})


class _FailQuery(_EchoQuery):
    name = "fail_query"
    calls: list[Any] = []

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        type(self).calls.append(arguments["value"])
        return ToolOutcome.failure("upstream_action_failed", "query failed")


class _SlowQuery(_EchoQuery):
    name = "slow_query"

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        await asyncio.sleep(0.05)
        return ToolOutcome.success({"value": arguments["value"]})


class _NotifyEffect(BaseTool):
    name = "notify"
    program_kind = "effect"
    max_call_sites = 2
    arguments_schema = {
        "type": "object",
        "properties": {"message": {"type": "string"}},
        "required": ["message"],
        "additionalProperties": False,
    }
    result_schema = {
        "type": "object",
        "properties": {"accepted": {"type": "boolean"}},
        "additionalProperties": False,
    }
    calls: list[dict] = []

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        type(self).calls.append({"arguments": dict(arguments), "context": context})
        return ToolOutcome.success({"accepted": True})


class _FailEffect(_NotifyEffect):
    name = "fail_effect"
    calls: list[dict] = []

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        type(self).calls.append({"arguments": dict(arguments), "context": context})
        return ToolOutcome.failure(
            "upstream_action_failed", "effect failed", status="failed"
        )


class _CreateTaskEffect(BaseTool):
    name = "task_create"
    program_kind = "effect"
    arguments_schema = {
        "type": "object",
        "properties": {"description": {"type": "string"}},
        "required": ["description"],
        "additionalProperties": False,
    }
    result_schema = {
        "type": "object",
        "properties": {"task_id": {"type": "string"}},
        "additionalProperties": False,
    }

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        task_id = "T_NEW"
        return ToolOutcome.success(
            {"task_id": task_id},
            emitted_events=[
                ToolGeneratedEvent(
                    event_type="agent.task_created",
                    payload={
                        "task_id": task_id,
                        "triggered_by_event_id": context.get("triggered_by_event_id"),
                    },
                )
            ],
        )


class _LongTextQuery(BaseTool):
    name = "long_text"
    program_kind = "query"
    text_value: str = "x" * 8000
    arguments_schema = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    result_schema = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "additionalProperties": False,
    }

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        return ToolOutcome.success({"text": type(self).text_value})


class _TaskLikeEffect(BaseTool):
    """schema 自带业务 task_id 的 effect（形如 task 工具的 note/complete 分支）。"""

    name = "task_like"
    program_kind = "effect"
    arguments_schema = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "note": {"type": "string"},
        },
        "required": ["task_id", "note"],
        "additionalProperties": False,
    }
    result_schema = {
        "type": "object",
        "properties": {"task_id": {"type": "string"}},
        "additionalProperties": False,
    }
    calls: list[dict] = []

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        type(self).calls.append({"arguments": dict(arguments), "context": context})
        return ToolOutcome.success({"task_id": arguments["task_id"]})


class _HostValueQuery(BaseTool):
    name = "host_value"
    program_kind = "query"
    arguments_schema = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    result_schema = {
        "type": "object",
        "properties": {"value": {}},
        "additionalProperties": False,
    }

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        class _SecretHost:
            def __repr__(self) -> str:
                return "SECRET_HOST_REPR"

        return ToolOutcome.success({"value": _SecretHost()})


def _registry(*tools: type[BaseTool]) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return registry


async def _execute(
    source: str,
    registry: ToolRegistry,
    *,
    call_timeout: float = 0.2,
    program_timeout: float = 0.5,
):
    prepared = preflight(source, registry, "group")
    executor = ProgramExecutor(
        registry=registry,
        session_factory=object(),
        scope_key="group:1",
        correlation_id="CORR",
        decision_id="DECISION",
        call_timeout_seconds=call_timeout,
        program_timeout_seconds=program_timeout,
    )
    return await executor.execute(prepared)


class ProgramReadOnlyAbiContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        _MembersQuery.calls.clear()
        _EchoQuery.calls.clear()

    async def test_query_values_support_fields_comprehension_join_and_fstring(
        self,
    ) -> None:
        result = await _execute(
            "\n".join(
                [
                    "result = members()",
                    "names = [m.card or m.nickname for m in result.members]",
                    "spoken = f'members: {join(\"、\", names)}'",
                    'return {"names": names, "spoken": spoken}',
                ]
            ),
            _registry(_MembersQuery),
        )
        self.assertEqual(
            result.result,
            {"names": ["Alice", "B"], "spoken": "members: Alice、B"},
        )
        self.assertEqual(result.trace.query_calls, ["members"])
        self.assertEqual(len(result.trace.calls), 1)
        self.assertEqual(result.trace.calls[0].status, "ok")

    async def test_declared_but_missing_field_reads_as_none(self) -> None:
        result = await _execute(
            "result = members()\nreturn result.members[0].card",
            _registry(_MembersQuery),
        )
        self.assertTrue(result.has_result)
        self.assertIsNone(result.result)

    async def test_comment_only_program_is_a_successful_empty_program(self) -> None:
        result = await _execute("# intentionally idle", ToolRegistry())
        self.assertFalse(result.has_result)
        self.assertIsNone(result.result)
        self.assertEqual(result.trace.query_calls, [])
        self.assertEqual(result.trace.effect_call_ids, [])

    async def test_upstream_long_text_uses_result_side_quota(self) -> None:
        """webfetch/websearch 级别的长正文（8000 字）必须能被程序读到并切片；
        体积检查走结果侧上限，不得用程序侧 4000 把合法结果整段拒掉。"""
        result = await _execute(
            "\n".join(
                [
                    "page = long_text()",
                    'return {"head": page.text[0:20], "size": len(page.text)}',
                ]
            ),
            _registry(_LongTextQuery),
        )
        self.assertEqual(result.result, {"head": "x" * 20, "size": 8000})

    async def test_upstream_text_over_result_side_quota_fails(self) -> None:
        with patch.object(_LongTextQuery, "text_value", "x" * 20_001):
            with self.assertRaises(ProgramExecutionError) as caught:
                await _execute(
                    "page = long_text()\nreturn len(page.text)",
                    _registry(_LongTextQuery),
                )
        self.assertEqual(caught.exception.info.error_kind, "program_quota_exceeded")
        self.assertEqual(
            caught.exception.info.details["quota"], "result_string_chars"
        )

    async def test_deep_nested_return_fails_with_value_depth(self) -> None:
        """深嵌套返回值必须以契约错误中止（value_depth 配额），不允许以
        RecursionError 等宿主异常逃出执行器——那会让该拍没有 program
        terminal。"""
        source = "\n".join(
            [
                "acc = 0",
                f"for item in [{', '.join('0' for _ in range(20))}]:",
                "    acc = [acc]",
                "return acc",
            ]
        )
        with self.assertRaises(ProgramExecutionError) as caught:
            await _execute(source, ToolRegistry())
        self.assertEqual(caught.exception.info.error_kind, "program_quota_exceeded")
        self.assertEqual(caught.exception.info.details["quota"], "value_depth")

    async def test_tool_cannot_leak_host_object_or_its_repr(self) -> None:
        with self.assertRaises(ProgramExecutionError) as caught:
            await _execute(
                "result = host_value()\nreturn result.value",
                _registry(_HostValueQuery),
            )
        self.assertEqual(
            caught.exception.info.error_kind, "program_forbidden_construct"
        )
        self.assertNotIn("SECRET_HOST_REPR", caught.exception.info.message)


class ProgramEffectContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        _NotifyEffect.calls.clear()
        _FailEffect.calls.clear()
        _EchoQuery.calls.clear()
        _FailQuery.calls.clear()
        _TaskLikeEffect.calls.clear()

    async def test_effect_is_persisted_as_intent_then_terminal(self) -> None:
        handle = EffectCallHandle(
            tool_call_id="TC1",
            called_event_id="CALLED1",
            decision_id="DECISION",
            tool_name="notify",
            task_id=None,
            call_site="1:0:notify:1",
            occurrence=1,
        )
        with (
            patch(
                "qqbot.services.agent_loop.program_runtime.begin_effect_call",
                new=AsyncMock(return_value=handle),
            ) as begin,
            patch(
                "qqbot.services.agent_loop.program_runtime.finish_effect_call",
                new=AsyncMock(return_value="TERMINAL1"),
            ) as finish,
        ):
            result = await _execute(
                'outcome = notify(message="hello")\nreturn outcome.accepted',
                _registry(_NotifyEffect),
            )

        self.assertTrue(result.result)
        self.assertEqual(result.trace.effect_call_ids, ["TC1"])
        self.assertEqual(len(_NotifyEffect.calls), 1)
        begin.assert_awaited_once()
        finish.assert_awaited_once()
        self.assertTrue(finish.await_args.kwargs["outcome"].ok)
        self.assertEqual(finish.await_args.kwargs["handle"].called_event_id, "CALLED1")

    async def test_query_failure_aborts_before_later_effect(self) -> None:
        with patch(
            "qqbot.services.agent_loop.program_runtime.begin_effect_call",
            new=AsyncMock(),
        ) as begin:
            with self.assertRaises(ProgramExecutionError) as caught:
                await _execute(
                    'value = fail_query(value="x")\nnotify(message="never")',
                    _registry(_FailQuery, _NotifyEffect),
                )
        self.assertEqual(caught.exception.info.error_kind, "upstream_action_failed")
        self.assertEqual(caught.exception.failed_call.name, "fail_query")
        begin.assert_not_awaited()
        self.assertEqual(_NotifyEffect.calls, [])

    async def test_effect_failure_writes_terminal_and_aborts_later_query(self) -> None:
        handle = EffectCallHandle(
            tool_call_id="TC_FAIL",
            called_event_id="CALLED_FAIL",
            decision_id="DECISION",
            tool_name="fail_effect",
            task_id=None,
            call_site="1:0:fail_effect:1",
            occurrence=1,
        )
        with (
            patch(
                "qqbot.services.agent_loop.program_runtime.begin_effect_call",
                new=AsyncMock(return_value=handle),
            ),
            patch(
                "qqbot.services.agent_loop.program_runtime.finish_effect_call",
                new=AsyncMock(return_value="TERMINAL_FAIL"),
            ) as finish,
        ):
            with self.assertRaises(ProgramExecutionError) as caught:
                await _execute(
                    'fail_effect(message="x")\nlater = echo(value="never")',
                    _registry(_FailEffect, _EchoQuery),
                )
        self.assertEqual(caught.exception.failed_call.name, "fail_effect")
        self.assertEqual(_EchoQuery.calls, [])
        finish.assert_awaited_once()
        self.assertFalse(finish.await_args.kwargs["outcome"].ok)

    async def test_declared_business_task_id_is_not_a_reserved_anchor(self) -> None:
        """schema 已声明的业务 task_id 只进 arguments，不进保留挂靠通道——
        与静态层 reserved = 保留名 - declared 同一口径。否则形如
        task(action=\"note\"/\"complete\") 的调用会伴生一条伪造的
        task_state_changed(pending→running)，把已收束任务复活。"""
        handle = EffectCallHandle(
            tool_call_id="TC_TASKLIKE",
            called_event_id="CALLED_TASKLIKE",
            decision_id="DECISION",
            tool_name="task_like",
            task_id=None,
            call_site="1:0:task_like:1",
            occurrence=1,
        )
        with (
            patch(
                "qqbot.services.agent_loop.program_runtime.begin_effect_call",
                new=AsyncMock(return_value=handle),
            ) as begin,
            patch(
                "qqbot.services.agent_loop.program_runtime.finish_effect_call",
                new=AsyncMock(return_value="TERM_TASKLIKE"),
            ),
        ):
            result = await _execute(
                'done = task_like(task_id="T1", note="进展")\nreturn done.task_id',
                _registry(_TaskLikeEffect),
            )

        self.assertEqual(result.result, "T1")
        begin_kwargs = begin.await_args.kwargs
        self.assertIsNone(begin_kwargs["task_id"])
        self.assertEqual(begin_kwargs["arguments"]["task_id"], "T1")
        self.assertEqual(_TaskLikeEffect.calls[0]["arguments"]["task_id"], "T1")
        self.assertIsNone(_TaskLikeEffect.calls[0]["context"]["task_id"])

    async def test_effect_result_variable_can_anchor_later_effect(self) -> None:
        handles = [
            EffectCallHandle(
                tool_call_id="TC_TASK",
                called_event_id="CALLED_TASK",
                decision_id="DECISION",
                tool_name="task_create",
                task_id=None,
                call_site="1:0:task_create:1",
                occurrence=1,
            ),
            EffectCallHandle(
                tool_call_id="TC_NOTIFY",
                called_event_id="CALLED_NOTIFY",
                decision_id="DECISION",
                tool_name="notify",
                task_id="T_NEW",
                call_site="2:0:notify:1",
                occurrence=1,
            ),
        ]
        with (
            patch(
                "qqbot.services.agent_loop.program_runtime.begin_effect_call",
                new=AsyncMock(side_effect=handles),
            ) as begin,
            patch(
                "qqbot.services.agent_loop.program_runtime.finish_effect_call",
                new=AsyncMock(side_effect=["TERM_TASK", "TERM_NOTIFY"]),
            ),
        ):
            result = await _execute(
                "\n".join(
                    [
                        'task = task_create(description="work", '
                        'triggered_by_event_id="EV1")',
                        'notify(message="done", task_id=task.task_id)',
                        "return task.task_id",
                    ]
                ),
                _registry(_CreateTaskEffect, _NotifyEffect),
            )

        self.assertEqual(result.result, "T_NEW")
        self.assertEqual(result.trace.effect_call_ids, ["TC_TASK", "TC_NOTIFY"])
        second_call = begin.await_args_list[1].kwargs
        self.assertEqual(second_call["task_id"], "T_NEW")
        self.assertEqual(second_call["triggered_by_event_id"], "EV1")
        self.assertEqual(_NotifyEffect.calls[0]["context"]["task_id"], "T_NEW")
        self.assertEqual(
            _NotifyEffect.calls[0]["context"]["triggered_by_event_id"], "EV1"
        )


class ProgramDynamicQuotaContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        _EchoQuery.calls.clear()

    async def test_dynamic_query_call_limit(self) -> None:
        source = "\n".join(
            [
                "items = [0, 1, 2, 3, 4, 5, 6]",
                "values = [echo(value=item).value for item in items]",
                "return values",
            ]
        )
        with self.assertRaises(ProgramExecutionError) as caught:
            await _execute(source, _registry(_EchoQuery))
        self.assertEqual(caught.exception.info.error_kind, "program_quota_exceeded")
        self.assertEqual(caught.exception.info.details["quota"], "query_calls")
        self.assertEqual(len(_EchoQuery.calls), 6)

    async def test_iteration_limit(self) -> None:
        source = (
            "total = 0\nfor item in [1, 2, 3]:\n    total = total + item\nreturn total"
        )
        with patch("qqbot.services.agent_loop.program_runtime.MAX_ITERATIONS", 2):
            with self.assertRaises(ProgramExecutionError) as caught:
                await _execute(source, ToolRegistry())
        self.assertEqual(caught.exception.info.details["quota"], "iterations")

    async def test_statement_limit(self) -> None:
        source = "one = 1\ntwo = 2\nreturn one + two"
        with patch("qqbot.services.agent_loop.program_runtime.MAX_STATEMENTS", 2):
            with self.assertRaises(ProgramExecutionError) as caught:
                await _execute(source, ToolRegistry())
        self.assertEqual(caught.exception.info.details["quota"], "statements")

    async def test_string_growth_is_checked_during_arithmetic(self) -> None:
        source = (
            'text = "a"\nfor item in ["x", "x", "x"]:\n'
            '    text = text + "xx"\nreturn text'
        )
        with patch("qqbot.services.agent_loop.program_runtime.MAX_STRING_LENGTH", 5):
            with self.assertRaises(ProgramExecutionError) as caught:
                await _execute(source, ToolRegistry())
        self.assertEqual(caught.exception.info.details["quota"], "string_chars")

    async def test_runtime_container_limit(self) -> None:
        with patch(
            "qqbot.services.agent_loop.program_runtime.MAX_CONTAINER_ELEMENTS", 2
        ):
            with self.assertRaises(ProgramExecutionError) as caught:
                await _execute("return [1, 2, 3]", ToolRegistry())
        self.assertEqual(caught.exception.info.details["quota"], "container_elements")

    async def test_return_byte_limit_fails_instead_of_truncating(self) -> None:
        with patch("qqbot.services.agent_loop.program_runtime.MAX_RETURN_BYTES", 8):
            with self.assertRaises(ProgramExecutionError) as caught:
                await _execute('return {"text": "long"}', ToolRegistry())
        self.assertEqual(caught.exception.info.error_kind, "program_output_too_large")
        self.assertGreater(caught.exception.info.details["actual_bytes"], 8)

    async def test_call_timeout(self) -> None:
        with self.assertRaises(ProgramExecutionError) as caught:
            await _execute(
                'return slow_query(value="x").value',
                _registry(_SlowQuery),
                call_timeout=0.01,
                program_timeout=0.2,
            )
        self.assertEqual(caught.exception.info.error_kind, "program_timeout")
        self.assertEqual(caught.exception.info.details["scope"], "call")

    async def test_program_timeout(self) -> None:
        with self.assertRaises(ProgramExecutionError) as caught:
            await _execute(
                'return slow_query(value="x").value',
                _registry(_SlowQuery),
                call_timeout=0.2,
                program_timeout=0.01,
            )
        self.assertEqual(caught.exception.info.error_kind, "program_timeout")
        self.assertEqual(caught.exception.info.details["scope"], "program")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

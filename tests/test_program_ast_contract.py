"""Static-contract tests for restricted Planner programs."""

# Test doubles intentionally expose immutable-by-convention class metadata.
# ruff: noqa: ARG002, RUF012

from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import patch

from qqbot.services.agent_loop.program_ast import (
    MAX_AST_NODES,
    MAX_CONTAINER_ELEMENTS,
    MAX_SOURCE_CHARS,
    MAX_STRING_LENGTH,
    ProgramPreflightError,
    preflight,
    strip_outer_fence,
)
from qqbot.services.agent_loop.tool_registry import (
    BaseTool,
    ToolOutcome,
    ToolRegistry,
)


class _MembersQuery(BaseTool):
    name = "members"
    program_kind = "query"
    allowed_scopes = ("group",)
    arguments_schema = {
        "type": "object",
        "properties": {"role": {"type": "string", "default": None}},
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

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        return ToolOutcome.success({"members": []})


class _NotifyEffect(BaseTool):
    name = "notify"
    program_kind = "effect"
    max_call_sites = 2
    allowed_scopes = ("group",)
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

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        return ToolOutcome.success({"accepted": True})


class _SendMessagesEffect(BaseTool):
    name = "send_messages"
    program_kind = "effect"
    max_call_sites = 2
    allowed_scopes = ("group",)
    arguments_schema = {
        "type": "object",
        "properties": {"messages": {"type": "array", "items": {}}},
        "required": ["messages"],
        "additionalProperties": False,
    }
    result_schema = {
        "type": "object",
        "properties": {"status": {"type": "string"}},
        "additionalProperties": False,
    }

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        return ToolOutcome.success({"status": "sent"})


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(_MembersQuery)
    registry.register(_NotifyEffect)
    registry.register(_SendMessagesEffect)
    return registry


class ProgramFenceContractTests(unittest.TestCase):
    def test_accepts_bare_python_and_both_outer_fences(self) -> None:
        expected = '# decide nothing\nreturn {"ok": True}'
        self.assertEqual(strip_outer_fence(expected), expected)
        self.assertEqual(strip_outer_fence(f"```python\n{expected}\n```"), expected)
        self.assertEqual(strip_outer_fence(f"```\n{expected}\n```"), expected)

    def test_rejects_prose_outside_or_extra_fences(self) -> None:
        for source in (
            "Here is the program:\n```python\nreturn None\n```",
            "```python\nreturn None\n```\nextra",
            "```python\n```text\nx\n```\n```",
        ):
            with self.subTest(source=source):
                with self.assertRaises(ProgramPreflightError) as caught:
                    strip_outer_fence(source)
                self.assertEqual(
                    caught.exception.info.error_kind, "program_syntax_error"
                )


class ProgramWhitelistContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = _registry()

    def _error(self, source: str, scope: str = "group"):
        with self.assertRaises(ProgramPreflightError) as caught:
            preflight(source, self.registry, scope)
        return caught.exception.info

    def test_common_query_transform_return_program_passes(self) -> None:
        prepared = preflight(
            "\n".join(
                [
                    'result = members(role="admin")',
                    "names = [m.card or m.nickname for m in result.members]",
                    'return {"names": names, "spoken": join("、", names)}',
                ]
            ),
            self.registry,
            "group",
        )
        self.assertTrue(prepared.has_return)
        self.assertEqual([site.name for site in prepared.call_sites], ["members"])

    def test_normal_send_messages_payload_fits_depth_limit(self) -> None:
        source = """send_messages(
    messages=[{"kind": "chat", "content": [
        {"type": "text", "data": {"text": "hello"}}
    ]}],
    triggered_by_event_id="01JZQ8",
)"""
        prepared = preflight(source, self.registry, "group")
        self.assertEqual(prepared.call_sites[0].name, "send_messages")

    def test_forbidden_constructs_are_rejected(self) -> None:
        cases = {
            "import": "import os",
            "while": "while True:\n    pass",
            "def": "def helper():\n    return 1",
            "lambda": "fn = lambda x: x",
            "try": "try:\n    x = 1\nexcept Exception:\n    x = 2",
            "raise": 'raise RuntimeError("x")',
            "with": "with resource:\n    x = 1",
            "del": "x = 1\ndel x",
            "assert": "assert True",
            "await": 'await notify(message="x")',
            "method": 'text = "x"\ntext.upper()',
            "dunder": "result = members()\nreturn result.__class__",
            "subscript_write": "items = [1]\nitems[0] = 2",
            "star_unpack": "items = [1]\ncopy = [*items]",
            "set": "items = {1, 2}",
            "set_comp": "items = {x for x in [1]}",
            "nested_comp": "items = [x for row in [[1]] for x in row]",
            "pow": "value = 2 ** 3",
            "sequence_multiply": 'value = "x" * 2',
        }
        for label, source in cases.items():
            with self.subTest(label=label):
                info = self._error(source)
                self.assertEqual(info.error_kind, "program_forbidden_construct")

    def test_unavailable_host_names_have_stable_error_kind(self) -> None:
        for name in ("eval", "exec", "getattr", "type", "range", "open"):
            with self.subTest(name=name):
                info = self._error(f"return {name}(1)")
                self.assertEqual(info.error_kind, "program_unknown_name")
                self.assertEqual(info.details["name"], name)

    def test_multiline_string_is_rejected_at_model_source_line(self) -> None:
        info = self._error('x = 1\ntext = """a\nb"""\nreturn text')
        self.assertEqual(info.error_kind, "program_forbidden_construct")
        self.assertEqual(info.details["construct"], "multiline_string")
        self.assertEqual(info.line, 2)

    def test_effects_cannot_hide_in_loops_or_comprehensions(self) -> None:
        for source in (
            'for item in ["a"]:\n    notify(message=item)',
            'calls = [notify(message=item) for item in ["a"]]',
        ):
            with self.subTest(source=source):
                info = self._error(source)
                self.assertEqual(
                    info.details["construct"],
                    "effect_in_loop_or_comprehension",
                )

    def test_return_cannot_hide_in_loop(self) -> None:
        info = self._error("for item in [1]:\n    return item")
        self.assertEqual(info.details["construct"], "return_in_loop")

    def test_effect_call_site_quota_is_static(self) -> None:
        info = self._error(
            'notify(message="a")\nnotify(message="b")\nnotify(message="c")'
        )
        self.assertEqual(info.error_kind, "program_quota_exceeded")
        self.assertEqual(info.details["quota"], "effect_call_sites:notify")
        self.assertEqual(info.details["actual"], 3)
        self.assertEqual(info.details["max"], 2)

    def test_program_functions_require_named_arguments(self) -> None:
        info = self._error('notify("hello")')
        self.assertEqual(info.details["construct"], "program_function_positional_args")

    def test_query_rejects_system_reserved_arguments(self) -> None:
        info = self._error('members(task_id="T1")')
        self.assertEqual(info.details["construct"], "query_reserved_argument")

    def test_effect_reserved_arguments_must_be_string_or_null(self) -> None:
        for source in (
            'notify(message="x", task_id=123)',
            'notify(message="x", triggered_by_event_id=True)',
        ):
            with self.subTest(source=source):
                info = self._error(source)
                self.assertEqual(info.details["construct"], "reserved_argument_type")

    def test_fstring_only_accepts_json_scalars(self) -> None:
        info = self._error('result = members()\ntext = f"{result.members}"')
        self.assertEqual(info.details["construct"], "fstring_non_scalar")

    def test_scope_hidden_function_is_unknown_during_preflight(self) -> None:
        info = self._error("result = members()", scope="system")
        self.assertEqual(info.error_kind, "program_unknown_name")
        self.assertEqual(info.details["name"], "members")

    def test_schema_field_reads_are_checked_statically(self) -> None:
        preflight(
            "result = members()\nreturn result.members[0].nickname",
            self.registry,
            "group",
        )
        info = self._error("result = members()\nreturn result.secret")
        self.assertEqual(info.error_kind, "program_unknown_field")
        self.assertEqual(info.details["function"], "members")
        self.assertEqual(info.details["field"], "secret")

    def test_call_sites_are_numbered_in_source_order(self) -> None:
        prepared = preflight(
            "first = members()\nsecond = members()\nreturn len(first.members)",
            self.registry,
            "group",
        )
        self.assertEqual([site.occurrence for site in prepared.call_sites], [1, 2])
        self.assertTrue(
            all(
                site.call_site.endswith(f":members:{site.occurrence}")
                for site in prepared.call_sites
            )
        )


class ProgramStaticQuotaContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = _registry()

    def _quota(self, source: str):
        with self.assertRaises(ProgramPreflightError) as caught:
            preflight(source, self.registry, "group")
        self.assertEqual(caught.exception.info.error_kind, "program_quota_exceeded")
        return caught.exception.info.details

    def test_source_character_limit(self) -> None:
        details = self._quota("#" + ("x" * MAX_SOURCE_CHARS))
        self.assertEqual(details["quota"], "source_chars")

    def test_ast_node_limit(self) -> None:
        source = "items = [" + ",".join("0" for _ in range(MAX_AST_NODES)) + "]"
        details = self._quota(source)
        self.assertEqual(details["quota"], "ast_nodes")

    def test_container_element_limit_is_independently_enforced(self) -> None:
        source = (
            "items = [" + ",".join("0" for _ in range(MAX_CONTAINER_ELEMENTS + 1)) + "]"
        )
        with patch(
            "qqbot.services.agent_loop.program_ast.MAX_AST_NODES",
            MAX_CONTAINER_ELEMENTS * 2,
        ):
            details = self._quota(source)
        self.assertEqual(details["quota"], "container_elements")

    def test_string_character_limit(self) -> None:
        details = self._quota('text = "' + ("x" * (MAX_STRING_LENGTH + 1)) + '"')
        self.assertEqual(details["quota"], "string_chars")

    def test_model_visible_nesting_limit(self) -> None:
        source = "value = 0\n"
        indent = ""
        for _ in range(9):
            source += f"{indent}if True:\n"
            indent += "    "
        source += f"{indent}value = 1\nreturn value"
        details = self._quota(source)
        self.assertEqual(details["quota"], "syntax_depth")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

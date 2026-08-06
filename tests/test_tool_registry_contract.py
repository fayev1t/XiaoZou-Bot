"""Contracts for the single Program API registry."""

# The small registry double exposes immutable-by-convention class metadata.
# ruff: noqa: ARG002, RUF012

from __future__ import annotations

import unittest
from typing import Any

from qqbot.core.permissions import PermissionTier
from qqbot.services.agent_loop.tool_registry import (
    BaseTool,
    ToolOutcome,
    ToolRegistry,
    get_tool_program_kind,
)
from qqbot.services.agent_loop.tools import build_default_registry


class _StubTool(BaseTool):
    name = "stub"
    description = "stub description"
    usage_prompt = "stub usage"
    arguments_schema = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
        "additionalProperties": False,
    }
    result_schema = {
        "type": "object",
        "properties": {"echo": {"type": "string"}},
        "required": ["echo"],
        "additionalProperties": False,
    }

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        return ToolOutcome.success({"echo": arguments["value"]})


class ToolRegistryContractTest(unittest.TestCase):
    def test_register_builds_program_spec_and_fresh_instances(self) -> None:
        registry = ToolRegistry()
        registry.register(_StubTool)

        spec = registry.spec("stub")
        assert spec is not None
        self.assertEqual(spec.program_kind, "effect")
        self.assertEqual(spec.max_call_sites, 2)
        self.assertEqual(spec.required_permission, PermissionTier.GUEST)
        self.assertIn("task_id=None", spec.signature)
        self.assertIn("triggered_by_event_id=None", spec.signature)
        self.assertIsNot(registry.get("stub"), registry.get("stub"))

    def test_query_signature_has_no_reserved_effect_parameters(self) -> None:
        class _Query(_StubTool):
            name = "query_stub"
            program_kind = "query"

        registry = ToolRegistry()
        registry.register(_Query)
        spec = registry.spec("query_stub")
        assert spec is not None
        self.assertNotIn("task_id", spec.signature)
        self.assertNotIn("triggered_by_event_id", spec.signature)

    def test_scope_filtering_uses_same_specs_as_runtime(self) -> None:
        class _GroupOnly(_StubTool):
            name = "group_only"
            allowed_scopes = ("group",)

        registry = ToolRegistry()
        registry.register(_StubTool)
        registry.register(_GroupOnly)
        self.assertEqual(registry.names("system"), ["stub"])
        self.assertEqual(registry.names("group"), ["group_only", "stub"])

    def test_usage_docs_are_complete_program_api_reference(self) -> None:
        registry = ToolRegistry()
        registry.register(_StubTool)
        rendered = registry.usage_docs("group")
        self.assertIn("## 程序函数：stub", rendered)
        self.assertIn("stub(*, value", rendered)
        self.assertIn("参数 schema", rendered)
        self.assertIn("返回 schema", rendered)
        self.assertIn("stub usage", rendered)
        self.assertIn("只有 return 的 JSON", rendered)
        self.assertFalse(hasattr(registry, "catalog"))

    def test_registration_rejects_invalid_metadata(self) -> None:
        registry = ToolRegistry()

        class _BadKind(_StubTool):
            name = "bad_kind"
            program_kind = "background"

        with self.assertRaises(ValueError):
            registry.register(_BadKind)

        class _BadResult(_StubTool):
            name = "bad_result"
            result_schema = None

        with self.assertRaises(ValueError):
            registry.register(_BadResult)

        class _BadName(_StubTool):
            name = "bad-name"

        with self.assertRaises(ValueError):
            registry.register(_BadName)

    def test_default_program_api_contains_exactly_active_nineteen_tools(self) -> None:
        registry = build_default_registry()
        self.assertEqual(
            set(registry.names()),
            {
                "get_group_info",
                "get_member_info",
                "get_member_list",
                "get_pending_join_requests",
                "get_recent_thoughts",
                "kick",
                "leave_group",
                "look_at_image",
                "meme_collection",
                "poke",
                "reflect",
                "reply",
                "respond_to_group_join_request",
                "search_history",
                "send_messages",
                "task",
                "wait",
                "webfetch",
                "websearch",
            },
        )
        self.assertEqual(len(registry), 19)

    def test_all_active_tools_declare_machine_readable_result_abi(self) -> None:
        registry = build_default_registry()
        for spec in registry.specs():
            with self.subTest(tool=spec.name):
                self.assertIn(spec.program_kind, {"query", "effect"})
                self.assertIsInstance(spec.result_schema, dict)
                self.assertTrue(spec.result_schema)
                self.assertGreaterEqual(spec.max_call_sites, 1)

    def test_active_result_schema_keys_are_locked(self) -> None:
        expected_top_level = {
            "get_group_info": {
                "group_create_time",
                "group_id",
                "group_name",
                "group_remark",
                "max_member_count",
                "member_count",
            },
            "get_member_info": {
                "banned_until",
                "card",
                "join_time",
                "last_sent_time",
                "level",
                "nickname",
                "role",
                "title",
                "user_id",
            },
            "get_member_list": {"count", "matched", "members"},
            "get_pending_join_requests": {
                "group_id",
                "handled_recent_count",
                "may_be_incomplete",
                "pending_count",
                "requests",
            },
            "kick": {"applied", "group_id", "reject_add_request", "user_id"},
            "leave_group": {"group_id", "is_dismiss", "left"},
            "look_at_image": {"answer", "image_hash", "question"},
            "poke": {"group_id", "user_id"},
            "meme_collection": {
                "action",
                "already_saved",
                "already_saved_count",
                "batch",
                "deleted",
                "description",
                "failed_count",
                "file_hash",
                "previous_description",
                "recaptioned",
                "results",
                "saved",
                "saved_count",
            },
            "reply": {
                "flush_at",
                "hard_deadline",
                "reply_task_id",
                "revision",
                "state",
            },
            "respond_to_group_join_request": {
                "applied",
                "approve",
                "group_id",
                "request_event_id",
                "user_id",
            },
            "search_history": {
                "anchor_event_id",
                "items",
                "matched",
                "warnings",
            },
            "send_messages": {"message_ids", "sent_messages", "status"},
            "task": {"action", "state", "task_id"},
            "wait": {"note", "scheduled", "seconds", "wake_at"},
            "reflect": {"chars", "written"},
            "get_recent_thoughts": {"returned", "ticks"},
            "webfetch": {
                "content_type",
                "final_url",
                "status_code",
                "text",
                "title",
                "truncated",
                "url",
            },
            "websearch": {"engine", "query", "results", "warnings"},
        }
        expected_array_items = {
            ("get_member_list", "members"): {
                "banned_until",
                "card",
                "join_time",
                "last_sent_time",
                "nickname",
                "role",
                "user_id",
            },
            ("get_pending_join_requests", "requests"): {
                "comment",
                "nickname",
                "user_id",
            },
            ("meme_collection", "results"): {
                "already_saved",
                "description",
                "error",
                "error_kind",
                "file_hash",
                "retryable",
                "saved",
            },
            ("search_history", "items"): {
                "event_id",
                "kind",
                "occurred_at",
                "render",
            },
            ("send_messages", "sent_messages"): {
                "content",
                "image_hash",
                "index",
                "kind",
                "message_id",
                "receipt",
                "self_id",
                "status",
            },
            ("websearch", "results"): {
                "fetch_error",
                "fetched_text",
                "snippet",
                "title",
                "url",
            },
        }

        registry = build_default_registry()
        self.assertEqual(set(registry.names()), set(expected_top_level))
        for name, expected_keys in expected_top_level.items():
            with self.subTest(tool=name):
                spec = registry.spec(name)
                assert spec is not None
                properties = spec.result_schema.get("properties") or {}
                self.assertEqual(set(properties), expected_keys)
        for (name, field), expected_keys in expected_array_items.items():
            with self.subTest(tool=name, field=field):
                spec = registry.spec(name)
                assert spec is not None
                field_schema = spec.result_schema["properties"][field]
                item_properties = field_schema["items"].get("properties") or {}
                self.assertEqual(set(item_properties), expected_keys)

    def test_query_effect_partition_matches_contract(self) -> None:
        registry = build_default_registry()
        queries = {
            spec.name for spec in registry.specs() if spec.program_kind == "query"
        }
        self.assertEqual(
            queries,
            {
                "get_group_info",
                "get_member_info",
                "get_member_list",
                "get_pending_join_requests",
                "get_recent_thoughts",
                "look_at_image",
                "search_history",
                "webfetch",
                "websearch",
            },
        )
        self.assertEqual(get_tool_program_kind(registry.get("send_messages")), "effect")
        self.assertEqual(get_tool_program_kind(registry.get("poke")), "effect")
        # reflect 写事件、留终态记录 → effect（2026-08-03）。它读起来像"记笔记"，
        # 但产出一条 agent.reflection_written，不能划到无痕的 query 一侧。
        self.assertEqual(get_tool_program_kind(registry.get("reflect")), "effect")


if __name__ == "__main__":
    unittest.main()

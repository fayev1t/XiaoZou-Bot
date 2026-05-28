"""Freeze the v2 `agent_events` single-table schema and ULID generator.

Contract source: 开发文档/v2.0/事件系统设计.md §3
Ingest contract:  开发文档/v2.0/EventIngest契约.md §4.1

Static string assertions only; safe to run without a live database.
"""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class AgentEventsSchemaContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.model_text = (ROOT / "qqbot" / "models" / "agent_event.py").read_text(
            encoding="utf-8"
        )
        self.init_text = (ROOT / "qqbot" / "models" / "__init__.py").read_text(
            encoding="utf-8"
        )
        self.database_text = (ROOT / "qqbot" / "core" / "database.py").read_text(
            encoding="utf-8"
        )
        self.ids_text = (ROOT / "qqbot" / "core" / "ids.py").read_text(encoding="utf-8")
        self.design_text = (
            ROOT / "开发文档" / "v2.0" / "事件系统设计.md"
        ).read_text(encoding="utf-8")
        self.ingest_text = (
            ROOT / "开发文档" / "v2.0" / "EventIngest契约.md"
        ).read_text(encoding="utf-8")

    def test_tablename_matches_contract(self) -> None:
        self.assertIn('__tablename__ = "agent_events"', self.model_text)

    def test_model_defines_all_required_columns(self) -> None:
        for column in (
            "event_id",
            "occurred_at",
            "origin",
            "type",
            "scope",
            "group_id",
            "user_id",
            "visibility",
            "correlation_id",
            "causation_id",
            "idempotency_key",
            "payload",
            "raw",
        ):
            with self.subTest(column=column):
                self.assertIn(column, self.model_text)

    def test_event_id_is_primary_key(self) -> None:
        self.assertRegex(self.model_text, r"event_id\s*=.*primary_key=True")

    def test_idempotency_key_is_unique(self) -> None:
        # 设计 §3 与 EventIngest §4.1 都要求 UNIQUE
        self.assertRegex(self.model_text, r"idempotency_key\s*=.*unique=True")

    def test_payload_is_required_jsonb(self) -> None:
        self.assertRegex(self.model_text, r"payload\s*=\s*Column\(JSONB,\s*nullable=False")

    def test_raw_is_optional_jsonb(self) -> None:
        self.assertRegex(self.model_text, r"raw\s*=\s*Column\(JSONB,\s*nullable=True")

    def test_occurred_at_is_timezone_aware(self) -> None:
        self.assertRegex(
            self.model_text,
            r"occurred_at\s*=\s*Column\(DateTime\(timezone=True\),\s*nullable=False",
        )

    def test_contract_indexes_declared(self) -> None:
        for idx in (
            "agent_events_scope_time_idx",
            "agent_events_corr_idx",
            "agent_events_caus_idx",
            "agent_events_type_time_idx",
        ):
            with self.subTest(idx=idx):
                self.assertIn(idx, self.model_text)

    def test_scope_time_index_covers_three_columns(self) -> None:
        self.assertIn(
            'Index("agent_events_scope_time_idx", "scope", "group_id", "occurred_at")',
            self.model_text,
        )

    def test_model_registered_in_init_db(self) -> None:
        # init_db() 必须显式引用 agent_event 模块以触发 Base.metadata 注册。
        # 当前实现走 `from qqbot.models import agent_event`（importlib 是
        # 历史方案）。两种形式都接受——只要事实上把模块加载进来。
        loads_module = (
            "from qqbot.models import agent_event" in self.database_text
            or 'importlib.import_module("qqbot.models.agent_event")'
            in self.database_text
        )
        self.assertTrue(
            loads_module,
            "init_db() must import qqbot.models.agent_event so its metadata "
            "gets registered onto Base before create_all runs.",
        )

    def test_model_exported_from_models_package(self) -> None:
        self.assertIn(
            "from qqbot.models.agent_event import AgentEvent", self.init_text
        )
        self.assertIn('"AgentEvent"', self.init_text)


class EventIdGeneratorContractTests(unittest.TestCase):
    def test_new_event_id_exists(self) -> None:
        from qqbot.core.ids import new_event_id

        token = new_event_id()
        self.assertEqual(len(token), 26)
        # Crockford base32 alphabet (no I, L, O, U)
        for ch in token:
            self.assertIn(ch, "0123456789ABCDEFGHJKMNPQRSTVWXYZ")

    def test_new_event_id_is_unique_across_a_batch(self) -> None:
        from qqbot.core.ids import new_event_id

        ids = {new_event_id() for _ in range(2000)}
        self.assertEqual(len(ids), 2000)

    def test_new_event_id_is_roughly_time_ordered(self) -> None:
        """Two ids drawn ≥ 2 ms apart must be lexicographically ordered."""
        import time as _time

        from qqbot.core.ids import new_event_id

        a = new_event_id()
        _time.sleep(0.003)
        b = new_event_id()
        self.assertLess(a, b)


class ContractDocsCrossReferenceTests(unittest.TestCase):
    """Sanity: the design docs that describe this schema still mention it."""

    def setUp(self) -> None:
        self.design_text = (
            ROOT / "开发文档" / "v2.0" / "事件系统设计.md"
        ).read_text(encoding="utf-8")
        self.ingest_text = (
            ROOT / "开发文档" / "v2.0" / "EventIngest契约.md"
        ).read_text(encoding="utf-8")

    def test_design_mentions_table_and_idempotency_key(self) -> None:
        self.assertIn("agent_events", self.design_text)
        self.assertIn("idempotency_key", self.design_text)

    def test_ingest_doc_mentions_idempotency_construction(self) -> None:
        self.assertIn("idempotency_key", self.ingest_text)
        self.assertIn("ON CONFLICT", self.ingest_text)


if __name__ == "__main__":
    unittest.main()

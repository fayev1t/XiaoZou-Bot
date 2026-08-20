"""raw_events is a disposable original-dump log.

Contract: 开发文档/v2.0/20-横切契约/EventIngest契约.md §7.0

No live DB. Fake sessions only.
"""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

_HAS_SQLALCHEMY = importlib.util.find_spec("sqlalchemy") is not None

ROOT = Path(__file__).resolve().parents[1]
DOCS_CONTRACTS = ROOT.parent / "开发文档" / "v2.0" / "20-横切契约"


def _table_name(stmt: Any) -> str:
    table = getattr(stmt, "table", None)
    return str(getattr(table, "name", "") or "")


def _insert_params(stmt: Any) -> dict[str, Any]:
    compiled = stmt.compile()
    return dict(getattr(compiled, "params", {}) or {})


class _RecordingSession:
    def __init__(self, sink: list[Any], *, fail: bool = False) -> None:
        self._sink = sink
        self._fail = fail

    async def execute(self, stmt: Any) -> Any:
        self._sink.append(stmt)
        if self._fail:
            raise RuntimeError("raw table missing")
        return SimpleNamespace(rowcount=1)

    async def commit(self) -> None:
        if self._fail:
            raise RuntimeError("raw table missing")

    async def __aenter__(self) -> "_RecordingSession":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None


class RawEventsSchemaContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.model_text = (ROOT / "qqbot" / "models" / "raw_event.py").read_text(
            encoding="utf-8"
        )
        self.init_text = (ROOT / "qqbot" / "models" / "__init__.py").read_text(
            encoding="utf-8"
        )
        self.database_text = (ROOT / "qqbot" / "core" / "database.py").read_text(
            encoding="utf-8"
        )
        self.ingest_text = (DOCS_CONTRACTS / "EventIngest契约.md").read_text(
            encoding="utf-8"
        )
        self.log_text = (
            ROOT / "qqbot" / "services" / "raw_event_log.py"
        ).read_text(encoding="utf-8")

    def test_tablename_and_columns(self) -> None:
        self.assertIn('__tablename__ = "raw_events"', self.model_text)
        for column in ("raw_id", "channel", "received_at", "raw_payload"):
            with self.subTest(column=column):
                self.assertIn(column, self.model_text)

    def test_no_status_column(self) -> None:
        self.assertNotRegex(self.model_text, r"\bstatus\s*=")
        self.assertNotRegex(self.model_text, r"\bprocessed\s*=")
        self.assertNotIn("is_processed", self.model_text)

    def test_model_registered(self) -> None:
        self.assertIn("from qqbot.models import raw_event", self.database_text)
        self.assertIn("from qqbot.models.raw_event import RawEvent", self.init_text)
        self.assertIn('"RawEvent"', self.init_text)

    def test_contract_describes_raw_table(self) -> None:
        self.assertIn("raw_events", self.ingest_text)
        self.assertIn("### 7.0", self.ingest_text)
        self.assertIn("满 100", self.ingest_text)

    def test_insert_records_failures(self) -> None:
        self.assertIn("insert_raw_event", self.log_text)
        self.assertIn("except Exception", self.log_text)
        self.assertIn("_RAW_CLEAR_AFTER", self.log_text)


class RuntimeDoesNotReadRawEventsTests(unittest.TestCase):
    """AgentLoop / projection / recovery never SELECT this table."""

    def test_runtime_paths_do_not_select_raw_events(self) -> None:
        roots = [
            ROOT / "qqbot" / "services" / "agent_loop",
            ROOT / "qqbot" / "services" / "event_ingest",
            ROOT / "qqbot" / "plugins",
        ]
        offenders: list[str] = []
        skip = {"raw_event_log.py"}
        for root in roots:
            for path in root.rglob("*.py"):
                if path.name in skip:
                    continue
                text = path.read_text(encoding="utf-8")
                if "raw_events" not in text and "RawEvent" not in text:
                    continue
                if "select(RawEvent" in text or "from raw_events" in text:
                    offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(offenders, [])


@unittest.skipUnless(_HAS_SQLALCHEMY, "sqlalchemy not installed")
class CopyRawEventContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_inserts_channel_and_payload(self) -> None:
        from qqbot.services.raw_event_log import copy_raw_event

        captured: list[Any] = []

        def factory() -> _RecordingSession:
            return _RecordingSession(captured)

        payload = {"post_type": "message", "raw_message": "hi"}
        await copy_raw_event(factory, channel="external", payload=payload)
        inserts = [
            stmt
            for stmt in captured
            if _insert_params(stmt).get("raw_payload") == payload
        ]
        self.assertEqual(len(inserts), 1)
        self.assertEqual(_table_name(inserts[0]), "raw_events")
        params = _insert_params(inserts[0])
        self.assertEqual(params.get("channel"), "external")
        self.assertEqual(params.get("raw_payload"), payload)
        self.assertTrue(params.get("raw_id"))
        self.assertIn("received_at", params)

    async def test_non_dict_payload_is_skipped(self) -> None:
        from qqbot.services.raw_event_log import copy_raw_event

        captured: list[Any] = []

        def factory() -> _RecordingSession:
            return _RecordingSession(captured)

        await copy_raw_event(factory, channel="external", payload="not-json")
        self.assertEqual(captured, [])

    async def test_insert_failure_is_swallowed(self) -> None:
        from qqbot.services.raw_event_log import copy_raw_event

        def factory() -> _RecordingSession:
            return _RecordingSession([], fail=True)

        await copy_raw_event(
            factory,
            channel="external",
            payload={"post_type": "message"},
        )


@unittest.skipUnless(_HAS_SQLALCHEMY, "sqlalchemy not installed")
class IngestCopiesRawThenContinuesTests(unittest.IsolatedAsyncioTestCase):
    async def test_copy_precedes_terminal_insert(self) -> None:
        from qqbot.services.event_ingest import EventIngest
        from qqbot.services.event_ingest.mappers import build_default_registry

        tables: list[str] = []

        class Session:
            async def execute(self, stmt: Any) -> Any:
                tables.append(_table_name(stmt))
                return SimpleNamespace(rowcount=1)

            async def commit(self) -> None:
                return None

            async def __aenter__(self) -> "Session":
                return self

            async def __aexit__(self, *args: Any) -> None:
                return None

        ingest = EventIngest(build_default_registry(), session_factory=Session)
        event = SimpleNamespace(
            post_type="message",
            message_type="group",
            sub_type="normal",
            time=1716700000,
            self_id=10000,
            message_id=12345,
            group_id=999,
            user_id=222,
            raw_message="hello",
            message=[SimpleNamespace(type="text", data={"text": "hello"})],
            sender=SimpleNamespace(
                user_id=222, nickname="alice", card="A", role="member"
            ),
            dict=lambda: {"post_type": "message", "raw_message": "hello"},
        )
        result = await ingest.ingest(event)
        self.assertEqual(result.status, "inserted")
        self.assertIn("raw_events", tables)
        self.assertIn("agent_events", tables)
        self.assertLess(tables.index("raw_events"), tables.index("agent_events"))

    async def test_copy_failure_blocks_registration(self) -> None:
        from qqbot.services.event_ingest import EventIngest
        from qqbot.services.event_ingest.mappers import build_default_registry

        tables: list[str] = []
        calls = {"n": 0}

        class Session:
            def __init__(self) -> None:
                calls["n"] += 1
                self._n = calls["n"]

            async def execute(self, stmt: Any) -> Any:
                if self._n == 1:
                    raise RuntimeError("raw_events dropped")
                tables.append(_table_name(stmt))
                return SimpleNamespace(rowcount=1)

            async def commit(self) -> None:
                return None

            async def __aenter__(self) -> "Session":
                return self

            async def __aexit__(self, *args: Any) -> None:
                return None

        ingest = EventIngest(build_default_registry(), session_factory=Session)
        event = SimpleNamespace(
            post_type="message",
            message_type="group",
            sub_type="normal",
            time=1716700000,
            self_id=10000,
            message_id=12345,
            group_id=999,
            user_id=222,
            raw_message="hello",
            message=[SimpleNamespace(type="text", data={"text": "hello"})],
            sender=SimpleNamespace(
                user_id=222, nickname="alice", card="A", role="member"
            ),
        )
        result = await ingest.ingest(event)
        self.assertEqual(result.status, "error")
        self.assertEqual(result.reason, "raw_insert_failed")
        self.assertEqual(tables, [])

    async def test_heartbeat_does_not_copy(self) -> None:
        from qqbot.services.event_ingest import EventIngest
        from qqbot.services.event_ingest import heartbeat as hb_mod
        from qqbot.services.event_ingest.mappers import build_default_registry

        called = {"n": 0}

        class Session:
            async def execute(self, stmt: Any) -> Any:
                called["n"] += 1
                raise AssertionError("heartbeat must not touch the database")

            async def __aenter__(self) -> "Session":
                return self

            async def __aexit__(self, *args: Any) -> None:
                return None

        ingest = EventIngest(build_default_registry(), session_factory=Session)
        event = SimpleNamespace(
            post_type="meta_event",
            meta_event_type="heartbeat",
            time=1716700000,
            self_id=10000,
            interval=5000,
            status={},
        )
        with tempfile.TemporaryDirectory() as tmp:
            original = hb_mod.HEARTBEAT_FILE
            hb_mod.HEARTBEAT_FILE = Path(tmp) / "heartbeat.json"
            try:
                result = await ingest.ingest(event)
            finally:
                hb_mod.HEARTBEAT_FILE = original
        self.assertEqual(result.status, "heartbeat")
        self.assertEqual(called["n"], 0)


if __name__ == "__main__":
    unittest.main()

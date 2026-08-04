from __future__ import annotations

import sys
import types
import unittest


class _Logger:
    def bind(self, **kwargs):
        return self

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


logging_mod = types.ModuleType("qqbot.core.logging")
logging_mod.get_logger = lambda name: _Logger()
sys.modules["qqbot.core.logging"] = logging_mod


class _Select:
    def where(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self


sqlalchemy = types.ModuleType("sqlalchemy")
sqlalchemy.select = lambda *args, **kwargs: _Select()
sqlalchemy.desc = lambda value: value
sqlalchemy_ext = types.ModuleType("sqlalchemy.ext")
sqlalchemy_asyncio = types.ModuleType("sqlalchemy.ext.asyncio")
sqlalchemy_asyncio.AsyncSession = object
sys.modules["sqlalchemy"] = sqlalchemy
sys.modules["sqlalchemy.ext"] = sqlalchemy_ext
sys.modules["sqlalchemy.ext.asyncio"] = sqlalchemy_asyncio


class _Column:
    def __eq__(self, other):
        return self

    def __le__(self, other):
        return self

    def __ge__(self, other):
        return self

    def desc(self):
        return self


class _AgentEvent:
    event_id = _Column()
    occurred_at = _Column()
    origin = _Column()
    type = _Column()
    scope = _Column()
    group_id = _Column()
    user_id = _Column()
    visibility = _Column()
    correlation_id = _Column()
    causation_id = _Column()
    payload = _Column()


agent_event_mod = types.ModuleType("qqbot.models.agent_event")
agent_event_mod.AgentEvent = _AgentEvent
sys.modules["qqbot.models.agent_event"] = agent_event_mod

event_writer_mod = types.ModuleType("qqbot.services.agent_loop.event_writer")


def _parse_scope_key(scope_key: str):
    if scope_key == "system":
        return "system", None
    scope, raw_id = scope_key.split(":", 1)
    return scope, int(raw_id)


event_writer_mod.parse_scope_key = _parse_scope_key
sys.modules["qqbot.services.agent_loop.event_writer"] = event_writer_mod

from tests import test_agent_loop_projection_contract as projection_tests


EXCLUDED = {
    "ProjectionSnapshotBoundaryTests",
    "RecapQueryTests",
    "SavedMemesAugmentTests",
}

loader = unittest.TestLoader()
suite = unittest.TestSuite()
for name, value in vars(projection_tests).items():
    if (
        isinstance(value, type)
        and issubclass(value, unittest.TestCase)
        and name not in EXCLUDED
    ):
        suite.addTests(loader.loadTestsFromTestCase(value))

result = unittest.TestResult()
suite.run(result)
print(f"ran={result.testsRun} failures={len(result.failures)} errors={len(result.errors)}")
for test, _ in result.failures:
    print(f"FAIL {test.id()}")
for test, error in result.errors:
    print(f"ERROR {test.id()}: {error.splitlines()[-1]}")
raise SystemExit(0 if result.wasSuccessful() else 1)

from __future__ import annotations

import asyncio
import sys
import types
import unittest
from dataclasses import dataclass, field


class _Logger:
    def bind(self, **kwargs):
        return self

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


logging_mod = types.ModuleType("qqbot.core.logging")
logging_mod.get_logger = lambda name: _Logger()
sys.modules["qqbot.core.logging"] = logging_mod

llm_mod = types.ModuleType("qqbot.core.llm")


async def _create_llm(**kwargs):
    return None


llm_mod.create_llm = _create_llm
sys.modules["qqbot.core.llm"] = llm_mod


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
event_writer_mod.parse_scope_key = lambda scope_key: (
    ("system", None)
    if scope_key == "system"
    else (scope_key.split(":", 1)[0], int(scope_key.split(":", 1)[1]))
)
sys.modules["qqbot.services.agent_loop.event_writer"] = event_writer_mod


class _Message:
    def __init__(self, content="", **kwargs):
        self.content = content
        self.usage_metadata = kwargs.get("usage_metadata")
        self.response_metadata = kwargs.get("response_metadata", {})


messages_mod = types.ModuleType("langchain_core.messages")
messages_mod.AIMessage = _Message
messages_mod.HumanMessage = _Message
messages_mod.SystemMessage = _Message
langchain_core_mod = types.ModuleType("langchain_core")
sys.modules["langchain_core"] = langchain_core_mod
sys.modules["langchain_core.messages"] = messages_mod


@dataclass
class _Snapshot:
    kind: str
    scope_key: str | None = None
    tick_seq: int | None = None
    correlation_id: str | None = None
    system_prompt: str = ""
    user_text: str = ""
    sections: list = field(default_factory=list)
    validation_retry: bool = False
    model: str | None = None
    attempts: list = field(default_factory=list)
    outcome: str | None = None

    def add_attempt(self, **kwargs):
        self.attempts.append(types.SimpleNamespace(**kwargs, error=None))


snapshot_mod = types.ModuleType("qqbot.services.agent_loop.prompt_snapshot")
snapshot_mod.PromptSnapshot = _Snapshot
snapshot_mod.extract_usage = lambda raw: None
snapshot_mod.section_stats = lambda sections: []
snapshot_mod.should_snapshot = lambda scope_key: False
snapshot_mod.write_snapshot = lambda snapshot: None
sys.modules["qqbot.services.agent_loop.prompt_snapshot"] = snapshot_mod

from tests import test_llm_planner_contract as planner_tests

suite = unittest.defaultTestLoader.loadTestsFromModule(planner_tests)
result = unittest.TestResult()
suite.run(result)
print(f"ran={result.testsRun} failures={len(result.failures)} errors={len(result.errors)}")
for test, _ in result.failures:
    print(f"FAIL {test.id()}")
for test, error in result.errors:
    print(f"ERROR {test.id()}: {error.splitlines()[-1]}")
raise SystemExit(0 if result.wasSuccessful() else 1)

"""本地桩运行器：在缺 sqlalchemy / loguru / langchain 的挂载工作区里跑
tests/test_reflection_contract.py。

与 _local_llm_runner.py / _local_projection_runner.py 同型（同一批桩 + 本文件
额外需要的 event_writer.write_runtime_event）。服务器上有完整依赖时直接
``python -m unittest tests.test_reflection_contract -v``，不需要本文件。
"""

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
sqlalchemy.__path__ = []  # 让 sqlalchemy.ext / .dialects 这类子模块可被解析
sqlalchemy.select = lambda *args, **kwargs: _Select()
sqlalchemy.desc = lambda value: value
sqlalchemy.func = types.SimpleNamespace(
    now=lambda: None, count=lambda *a, **k: None
)
sqlalchemy.literal = lambda value: value
sqlalchemy.and_ = lambda *args: None
sqlalchemy.or_ = lambda *args: None
sqlalchemy.delete = lambda *args, **kwargs: _Select()
sqlalchemy.update = lambda *args, **kwargs: _Select()
sqlalchemy.insert = lambda *args, **kwargs: _Select()
sqlalchemy.text = lambda value: value
sqlalchemy.Select = object
sqlalchemy_ext = types.ModuleType("sqlalchemy.ext")
sqlalchemy_ext.__path__ = []
sqlalchemy_asyncio = types.ModuleType("sqlalchemy.ext.asyncio")
sqlalchemy_asyncio.AsyncSession = object
sqlalchemy_dialects = types.ModuleType("sqlalchemy.dialects")
sqlalchemy_dialects.__path__ = []
sqlalchemy_pg = types.ModuleType("sqlalchemy.dialects.postgresql")
sqlalchemy_pg.insert = lambda *args, **kwargs: _Select()
sqlalchemy_pg.JSONB = object
sys.modules["sqlalchemy"] = sqlalchemy
sys.modules["sqlalchemy.ext"] = sqlalchemy_ext
sys.modules["sqlalchemy.ext.asyncio"] = sqlalchemy_asyncio
sys.modules["sqlalchemy.dialects"] = sqlalchemy_dialects
sys.modules["sqlalchemy.dialects.postgresql"] = sqlalchemy_pg


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


# 整棵 qqbot.models 打桩：真实模块逐个 import 大量 SQLAlchemy 列类型，
# 补齐 ORM 桩不如直接把模型换成朴素对象——本文件的被测代码只用到
# AgentEvent 的列名做查询构造。
models_pkg = types.ModuleType("qqbot.models")
models_pkg.__path__ = []
sys.modules["qqbot.models"] = models_pkg

agent_event_mod = types.ModuleType("qqbot.models.agent_event")
agent_event_mod.AgentEvent = _AgentEvent
sys.modules["qqbot.models.agent_event"] = agent_event_mod

for _name, _attr in (
    ("agent_delivery_claim", "AgentDeliveryClaim"),
    ("agent_image_caption", "AgentImageCaption"),
    ("agent_meme", "AgentMeme"),
    ("agent_task", "AgentTask"),
    ("base", "Base"),
):
    _mod = types.ModuleType(f"qqbot.models.{_name}")
    setattr(_mod, _attr, _AgentEvent)
    sys.modules[f"qqbot.models.{_name}"] = _mod
    setattr(models_pkg, _attr, _AgentEvent)


def _parse_scope_key(scope_key: str):
    if scope_key == "system":
        return "system", None, None
    if scope_key.startswith("group:"):
        return "group", int(scope_key.split(":", 1)[1]), None
    if scope_key.startswith("private:"):
        return "private", None, int(scope_key.split(":", 1)[1])
    raise ValueError(f"invalid scope_key: {scope_key!r}")


async def _write_runtime_event(session_factory, **kwargs):
    return "EV_STUB"


event_writer_mod = types.ModuleType("qqbot.services.agent_loop.event_writer")
event_writer_mod.parse_scope_key = _parse_scope_key
event_writer_mod.write_runtime_event = _write_runtime_event
event_writer_mod.write_agent_event = _write_runtime_event
event_writer_mod.write_agent_events = _write_runtime_event
event_writer_mod.write_internal_event = _write_runtime_event
event_writer_mod.SessionFactory = object
event_writer_mod.AgentEventWrite = object
event_writer_mod.RuntimeEventPublisher = object
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
sys.modules["langchain_core"] = types.ModuleType("langchain_core")
sys.modules["langchain_core.messages"] = messages_mod

snapshot_mod = types.ModuleType("qqbot.services.agent_loop.prompt_snapshot")
snapshot_mod.PromptSnapshot = object
snapshot_mod.extract_usage = lambda raw: None
snapshot_mod.section_stats = lambda sections: []
snapshot_mod.should_snapshot = lambda scope_key: False
snapshot_mod.write_snapshot = lambda snapshot: None
sys.modules["qqbot.services.agent_loop.prompt_snapshot"] = snapshot_mod

from tests import test_reflection_contract as reflection_tests  # noqa: E402

suite = unittest.defaultTestLoader.loadTestsFromModule(reflection_tests)
result = unittest.TestResult()
suite.run(result)
print(
    f"ran={result.testsRun} failures={len(result.failures)} "
    f"errors={len(result.errors)}"
)
for test, trace in result.failures:
    print(f"FAIL {test.id()}: {trace.splitlines()[-1]}")
for test, error in result.errors:
    print(f"ERROR {test.id()}: {error.splitlines()[-1]}")
raise SystemExit(0 if result.wasSuccessful() else 1)

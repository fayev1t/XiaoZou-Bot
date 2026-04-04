from __future__ import annotations

import asyncio
import importlib
import sys
import types
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


class _DummyHandler:
    def __init__(self, *, rule: object, priority: int, block: bool) -> None:
        self.rule = rule
        self.priority = priority
        self.block = block
        self.funcs: list[Any] = []

    def handle(self):
        def decorator(func):
            self.funcs.append(func)
            return func

        return decorator


class _DummyLogger:
    def __init__(self) -> None:
        self.records: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def info(self, *args: object, **kwargs: object) -> None:
        self.records.append(("info", args, dict(kwargs)))

    def warning(self, *args: object, **kwargs: object) -> None:
        self.records.append(("warning", args, dict(kwargs)))

    def error(self, *args: object, **kwargs: object) -> None:
        self.records.append(("error", args, dict(kwargs)))


class _FakeSession:
    def __init__(self) -> None:
        self.commit_calls = 0
        self.rollback_calls = 0

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        _ = exc_type, exc, tb

    async def commit(self) -> None:
        self.commit_calls += 1

    async def rollback(self) -> None:
        self.rollback_calls += 1


class _SessionFactory:
    def __init__(self) -> None:
        self.sessions: list[_FakeSession] = []

    def __call__(self) -> _FakeSession:
        session = _FakeSession()
        self.sessions.append(session)
        return session


class _AggregatorRecorder:
    def __init__(self) -> None:
        self.begin_calls: list[int] = []
        self.complete_calls: list[int] = []
        self.fail_calls: list[int] = []
        self.finish_calls: list[dict[str, Any]] = []

    async def begin_message_persist(self, group_id: int) -> None:
        self.begin_calls.append(group_id)

    async def complete_message_persist(self, group_id: int) -> None:
        self.complete_calls.append(group_id)

    async def fail_message_persist(self, group_id: int) -> None:
        self.fail_calls.append(group_id)

    async def finish_message_persist_and_add_message(self, **kwargs: Any) -> None:
        self.finish_calls.append(kwargs)
        await asyncio.sleep(0)


class _PipelineState:
    def __init__(self) -> None:
        self.saved_id = 321
        self.persist_raw_calls: list[dict[str, Any]] = []
        self.format_and_update_calls: list[dict[str, Any]] = []
        self.format_started = asyncio.Event()
        self.format_release = asyncio.Event()


class _MessageSegment:
    def __init__(self, segment_type: str, data: dict[str, object] | None = None) -> None:
        self.type = segment_type
        self.data = data or {}


class _Message(list[_MessageSegment]):
    def __str__(self) -> str:
        return "".join(str(segment.data.get("text", "")) for segment in self)


def _install_event_handlers_test_stubs() -> tuple[
    _DummyLogger,
    _SessionFactory,
    _AggregatorRecorder,
    _PipelineState,
]:
    logger = _DummyLogger()
    session_factory = _SessionFactory()
    aggregator = _AggregatorRecorder()
    pipeline_state = _PipelineState()

    qqbot_package = types.ModuleType("qqbot")
    setattr(qqbot_package, "__path__", [str(ROOT / "qqbot")])
    sys.modules["qqbot"] = qqbot_package

    plugins_package = types.ModuleType("qqbot.plugins")
    setattr(plugins_package, "__path__", [str(ROOT / "qqbot" / "plugins")])
    sys.modules["qqbot.plugins"] = plugins_package

    core_package = types.ModuleType("qqbot.core")
    setattr(core_package, "__path__", [str(ROOT / "qqbot" / "core")])
    sys.modules["qqbot.core"] = core_package

    services_package = types.ModuleType("qqbot.services")
    setattr(services_package, "__path__", [str(ROOT / "qqbot" / "services")])
    sys.modules["qqbot.services"] = services_package

    database_module = types.ModuleType("qqbot.core.database")
    setattr(database_module, "AsyncSessionLocal", session_factory)
    sys.modules["qqbot.core.database"] = database_module

    logging_module = types.ModuleType("qqbot.core.logging")
    setattr(logging_module, "get_logger", lambda name: logger)
    sys.modules["qqbot.core.logging"] = logging_module

    settings_module = types.ModuleType("qqbot.core.settings")
    setattr(settings_module, "get_bot_nicknames", lambda: ("小奏",))
    sys.modules["qqbot.core.settings"] = settings_module

    nonebot_module = types.ModuleType("nonebot")
    setattr(
        nonebot_module,
        "on_notice",
        lambda *, rule, priority, block: _DummyHandler(
            rule=rule,
            priority=priority,
            block=block,
        ),
    )
    setattr(
        nonebot_module,
        "on_message",
        lambda *, rule, priority, block: _DummyHandler(
            rule=rule,
            priority=priority,
            block=block,
        ),
    )
    sys.modules["nonebot"] = nonebot_module

    adapters_module = types.ModuleType("nonebot.adapters")

    class Event:
        pass

    setattr(adapters_module, "Event", Event)
    sys.modules["nonebot.adapters"] = adapters_module

    rule_module = types.ModuleType("nonebot.rule")

    class Rule:
        def __init__(self, checker):
            self.checkers = (checker,)

    setattr(rule_module, "Rule", Rule)
    sys.modules["nonebot.rule"] = rule_module

    onebot_v11_module = types.ModuleType("nonebot.adapters.onebot.v11")

    class Bot:
        pass

    class GroupMessageEvent(Event):
        pass

    class GroupIncreaseNoticeEvent(Event):
        pass

    class GroupDecreaseNoticeEvent(Event):
        pass

    class GroupRecallNoticeEvent(Event):
        pass

    setattr(onebot_v11_module, "Bot", Bot)
    setattr(onebot_v11_module, "GroupMessageEvent", GroupMessageEvent)
    setattr(onebot_v11_module, "GroupIncreaseNoticeEvent", GroupIncreaseNoticeEvent)
    setattr(onebot_v11_module, "GroupDecreaseNoticeEvent", GroupDecreaseNoticeEvent)
    setattr(onebot_v11_module, "GroupRecallNoticeEvent", GroupRecallNoticeEvent)
    sys.modules["nonebot.adapters.onebot.v11"] = onebot_v11_module

    for module_name, attr_name in (
        ("qqbot.services.user", "UserService"),
        ("qqbot.services.group", "GroupService"),
        ("qqbot.services.group_message", "GroupMessageService"),
        ("qqbot.services.group_member", "GroupMemberService"),
    ):
        module = types.ModuleType(module_name)
        setattr(module, attr_name, object)
        sys.modules[module_name] = module

    aggregator_module = types.ModuleType("qqbot.services.message_aggregator")
    setattr(aggregator_module, "message_aggregator", aggregator)
    sys.modules["qqbot.services.message_aggregator"] = aggregator_module

    message_pipeline_module = types.ModuleType("qqbot.services.message_pipeline")

    class MessagePipeline:
        def extract_raw(self, event: object) -> str:
            return str(getattr(event, "raw_message", ""))

        def create_raw_record(self, event: object, raw_message: str) -> object:
            return types.SimpleNamespace(
                group_id=getattr(event, "group_id"),
                user_id=getattr(event, "user_id"),
                msg_hash="msg-hash-123",
                onebot_message_id=str(getattr(event, "message_id", "")),
                raw_message=raw_message,
                formatted_message=None,
                timestamp=getattr(event, "time", None),
                message_type=None,
            )

        async def persist_raw(self, session: object, record: object) -> int:
            pipeline_state.persist_raw_calls.append(
                {
                    "session": session,
                    "record": record,
                }
            )
            return pipeline_state.saved_id

        async def format_and_update(
            self,
            session: object,
            event: object,
            saved_id: int,
            *,
            msg_hash: str,
            raw_message: str | None = None,
        ) -> object:
            pipeline_state.format_and_update_calls.append(
                {
                    "session": session,
                    "event": event,
                    "saved_id": saved_id,
                    "msg_hash": msg_hash,
                    "raw_message": raw_message,
                }
            )
            pipeline_state.format_started.set()
            await pipeline_state.format_release.wait()
            return types.SimpleNamespace(
                group_id=getattr(event, "group_id"),
                user_id=getattr(event, "user_id"),
                msg_hash=msg_hash,
                onebot_message_id=str(getattr(event, "message_id", "")),
                raw_message=raw_message,
                formatted_message="<System-Message>格式化完成</System-Message>",
                timestamp=getattr(event, "time", None),
                message_type="xml",
            )

    setattr(message_pipeline_module, "MessagePipeline", MessagePipeline)
    setattr(message_pipeline_module, "MessageRecord", object)
    sys.modules["qqbot.services.message_pipeline"] = message_pipeline_module

    return logger, session_factory, aggregator, pipeline_state


def _load_event_handlers_module() -> tuple[Any, _DummyLogger, _SessionFactory, _AggregatorRecorder, _PipelineState]:
    logger, session_factory, aggregator, pipeline_state = _install_event_handlers_test_stubs()
    sys.modules.pop("qqbot.plugins.event_handlers", None)
    module = importlib.import_module("qqbot.plugins.event_handlers")
    return module, logger, session_factory, aggregator, pipeline_state


class _FakeGroupMessageEvent:
    def __init__(self) -> None:
        self.group_id = 10001
        self.user_id = 20002
        self.self_id = 30003
        self.message_id = 40004
        self.time = object()
        self.raw_message = "第一条消息"
        self.original_message = None
        self.message = _Message([_MessageSegment("text", {"text": "第一条消息"})])
        self.to_me = False


class EventHandlersTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        (
            self.module,
            self.logger,
            self.session_factory,
            self.aggregator,
            self.pipeline_state,
        ) = _load_event_handlers_module()

    async def test_handle_group_message_persists_raw_then_adds_background_format_task(self) -> None:
        event = _FakeGroupMessageEvent()
        bot = self.module.Bot()

        await self.module.handle_group_message(bot, event)

        self.assertEqual(self.aggregator.begin_calls, [event.group_id])
        self.assertEqual(self.aggregator.complete_calls, [])
        self.assertEqual(self.aggregator.fail_calls, [])
        self.assertEqual(len(self.aggregator.finish_calls), 1)
        self.assertEqual(len(self.pipeline_state.persist_raw_calls), 1)
        self.assertEqual(self.session_factory.sessions[0].commit_calls, 1)

        finish_call = self.aggregator.finish_calls[0]
        self.assertEqual(finish_call["group_id"], event.group_id)
        self.assertEqual(finish_call["user_id"], event.user_id)
        self.assertEqual(finish_call["raw_message"], event.raw_message)
        self.assertIsNone(finish_call["formatted_message"])
        self.assertEqual(finish_call["persisted_message_id"], self.pipeline_state.saved_id)
        self.assertFalse(finish_call["is_bot_mentioned"])

        format_task = finish_call["format_task"]
        self.assertIsInstance(format_task, asyncio.Task)
        self.assertFalse(format_task.done())

        await asyncio.wait_for(self.pipeline_state.format_started.wait(), timeout=0.2)
        self.assertEqual(len(self.pipeline_state.format_and_update_calls), 1)
        self.assertGreaterEqual(len(self.session_factory.sessions), 2)
        self.assertEqual(self.session_factory.sessions[1].commit_calls, 0)

        self.pipeline_state.format_release.set()
        formatted_record = await asyncio.wait_for(format_task, timeout=0.2)

        self.assertEqual(
            formatted_record.formatted_message,
            "<System-Message>格式化完成</System-Message>",
        )
        self.assertEqual(self.session_factory.sessions[1].commit_calls, 1)


if __name__ == "__main__":
    unittest.main()

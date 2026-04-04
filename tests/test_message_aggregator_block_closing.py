from __future__ import annotations

import asyncio
import importlib
import sys
import types
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
_UNSET = object()


def _install_message_aggregator_test_stubs() -> None:
    class _DummyLogger:
        def debug(self, *args: object, **kwargs: object) -> None:
            _ = args, kwargs

        def info(self, *args: object, **kwargs: object) -> None:
            _ = args, kwargs

        def warning(self, *args: object, **kwargs: object) -> None:
            _ = args, kwargs

        def error(self, *args: object, **kwargs: object) -> None:
            _ = args, kwargs

    qqbot_package = types.ModuleType("qqbot")
    setattr(qqbot_package, "__path__", [str(ROOT / "qqbot")])
    sys.modules["qqbot"] = qqbot_package

    core_package = types.ModuleType("qqbot.core")
    setattr(core_package, "__path__", [str(ROOT / "qqbot" / "core")])
    sys.modules["qqbot.core"] = core_package

    services_package = types.ModuleType("qqbot.services")
    setattr(services_package, "__path__", [str(ROOT / "qqbot" / "services")])
    sys.modules["qqbot.services"] = services_package

    logging_module = types.ModuleType("qqbot.core.logging")
    setattr(logging_module, "get_logger", lambda name: _DummyLogger())
    setattr(logging_module, "log_ai_input", lambda *args, **kwargs: None)
    setattr(logging_module, "log_ai_output", lambda *args, **kwargs: None)
    setattr(logging_module, "log_event", lambda *args, **kwargs: None)
    sys.modules["qqbot.core.logging"] = logging_module

    prompt_module = types.ModuleType("qqbot.services.prompt")

    class _DummyPromptManager:
        @property
        def wait_time_judge_prompt(self) -> str:
            return "stub prompt"

    setattr(prompt_module, "PromptManager", _DummyPromptManager)
    sys.modules["qqbot.services.prompt"] = prompt_module


def _load_message_aggregator_module() -> Any:
    _install_message_aggregator_test_stubs()
    sys.modules.pop("qqbot.services.message_aggregator", None)
    return importlib.import_module("qqbot.services.message_aggregator")

@dataclass
class FakeEvent:
    self_id: int = 123456


class MessageAggregatorBlockClosingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.module = _load_message_aggregator_module()
        self.aggregator = self.module.MessageAggregator()
        self.aggregator._pre_close_quiet_seconds = 0.05
        self.group_id = 10001
        self._next_persisted_message_id = 1

    async def asyncTearDown(self) -> None:
        await self.aggregator.shutdown()

    async def _add_message(
        self,
        text: str,
        user_id: int = 1,
        *,
        formatted_message: str | None | object = _UNSET,
        format_task: asyncio.Task | None = None,
    ) -> None:
        persisted_message_id = self._next_persisted_message_id
        self._next_persisted_message_id += 1
        await self.aggregator.begin_message_persist(self.group_id)
        await self.aggregator.finish_message_persist_and_add_message(
            group_id=self.group_id,
            user_id=user_id,
            msg_hash=f"msg-{persisted_message_id}",
            raw_message=text,
            formatted_message=text if formatted_message is _UNSET else formatted_message,
            format_task=format_task,
            event=FakeEvent(),
            persisted_message_id=persisted_message_id,
        )

    async def _wait_for(self, predicate: Any, timeout: float = 0.5) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if predicate():
                return
            await asyncio.sleep(0.005)
        self.fail("condition not met within timeout")

    def _install_fake_judge(
        self,
        wait_seconds: float,
    ) -> list[tuple[int, int, int, list[str]]]:
        judge_calls: list[tuple[int, int, int, list[str]]] = []

        async def fake_judge(
            group_id: int,
            block: Any,
            expected_version: int,
            expected_judge_request_version: int,
        ) -> None:
            judge_calls.append(
                (
                    expected_version,
                    expected_judge_request_version,
                    id(block),
                    [msg.raw_message for msg in block.messages],
                )
            )
            await asyncio.sleep(0)
            lock = self.aggregator._get_lock(group_id)
            async with lock:
                if not self.aggregator._is_snapshot_current(
                    group_id,
                    block,
                    expected_version,
                    require_no_pending=True,
                    expected_judge_request_version=expected_judge_request_version,
                ):
                    return
                self.aggregator._schedule_wait_task(
                    group_id,
                    block,
                    expected_version,
                    wait_seconds,
                    expected_judge_request_version,
                )

        self.aggregator._judge_wait_time = fake_judge
        return judge_calls

    async def test_layer1_wait_judge_uses_raw_message_not_formatted_placeholder(self) -> None:
        captured_prompts: list[str] = []
        captured_logs: list[str] = []

        llm_module = types.ModuleType("qqbot.core.llm")

        class FakeLLM:
            async def ainvoke(self, messages: list[object]) -> object:
                captured_prompts.append(messages[0].content)
                return types.SimpleNamespace(
                    content='{"should_wait": false, "reason": "raw ok"}'
                )

        async def fake_create_llm(*args: object, **kwargs: object) -> FakeLLM:
            _ = args, kwargs
            return FakeLLM()

        setattr(llm_module, "create_llm", fake_create_llm)
        sys.modules["qqbot.core.llm"] = llm_module

        silence_module = types.ModuleType("qqbot.services.silence_mode")
        setattr(silence_module, "is_silent", lambda group_id: False)
        sys.modules["qqbot.services.silence_mode"] = silence_module

        messages_module = types.ModuleType("langchain_core.messages")

        class FakeHumanMessage:
            def __init__(self, content: str) -> None:
                self.content = content

        setattr(messages_module, "HumanMessage", FakeHumanMessage)
        sys.modules["langchain_core.messages"] = messages_module

        original_log_ai_input = self.module.log_ai_input
        self.module.log_ai_input = lambda layer, group_id, prompt: captured_logs.append(prompt)
        try:
            block = self.module.ResponseBlock(group_id=self.group_id)
            self.aggregator._blocks[self.group_id] = block
            block.add_message(
                self.module.PendingMessage(
                    user_id=1,
                    msg_hash="msg-1",
                    raw_message="原始CQ文本",
                    formatted_message="<System-Message>不该给Layer1</System-Message>",
                    event=FakeEvent(),
                )
            )
            await self.aggregator._judge_wait_time(
                self.group_id,
                block,
                expected_version=0,
                expected_judge_request_version=0,
            )
        finally:
            self.module.log_ai_input = original_log_ai_input

        self.assertEqual(len(captured_prompts), 1)
        self.assertIn("原始CQ文本", captured_prompts[0])
        self.assertNotIn("<System-Message>", captured_prompts[0])
        self.assertEqual(len(captured_logs), 1)
        self.assertIn("原始CQ文本", captured_logs[0])
        self.assertNotIn("<System-Message>", captured_logs[0])

    async def test_new_message_during_pre_close_quiet_joins_same_block_and_reruns_layer1(self) -> None:
        judge_calls = self._install_fake_judge(wait_seconds=0.0)
        callback_snapshots: list[tuple[int, list[str]]] = []
        callback_done = asyncio.Event()

        async def fake_reply(group_id: int, block: Any) -> None:
            _ = group_id
            callback_snapshots.append(
                (id(block), [msg.formatted_message for msg in block.messages])
            )
            callback_done.set()

        self.aggregator.set_reply_callback(fake_reply)

        await self._add_message("第一条")
        await self._wait_for(
            lambda: self.aggregator._blocks[self.group_id].wait_task is not None
        )

        original_block = self.aggregator._blocks[self.group_id]
        self.assertEqual(original_block.get_message_count(), 1)
        self.assertFalse(original_block.is_processing)
        self.assertEqual(len(judge_calls), 1)
        self.assertFalse(callback_done.is_set())

        await self._add_message("第二条")
        await self._wait_for(lambda: len(judge_calls) == 2)

        same_block = self.aggregator._blocks[self.group_id]
        self.assertIs(same_block, original_block)
        self.assertEqual(same_block.get_message_count(), 2)
        self.assertEqual(judge_calls[1][2], id(original_block))
        self.assertEqual(judge_calls[1][3], ["第一条", "第二条"])
        self.assertFalse(callback_done.is_set())

        await asyncio.wait_for(callback_done.wait(), timeout=0.5)
        self.assertEqual(
            callback_snapshots,
            [(id(original_block), ["第一条", "第二条"])],
        )

    async def test_new_message_after_block_freezes_creates_new_block(self) -> None:
        self._install_fake_judge(wait_seconds=0.0)
        callback_snapshots: list[tuple[int, list[str]]] = []
        first_processing_started = asyncio.Event()
        allow_first_callback_to_finish = asyncio.Event()
        callback_count = 0

        async def fake_reply(group_id: int, block: Any) -> None:
            _ = group_id
            nonlocal callback_count
            callback_count += 1
            callback_snapshots.append(
                (id(block), [msg.formatted_message for msg in block.messages])
            )
            if callback_count == 1:
                first_processing_started.set()
                await allow_first_callback_to_finish.wait()

        self.aggregator.set_reply_callback(fake_reply)

        await self._add_message("旧块消息")
        await asyncio.wait_for(first_processing_started.wait(), timeout=0.5)

        processing_block = self.aggregator._blocks[self.group_id]
        self.assertTrue(processing_block.is_processing)

        await self._add_message("新块消息")

        new_block = self.aggregator._blocks[self.group_id]
        self.assertIsNot(new_block, processing_block)
        self.assertEqual(new_block.get_message_count(), 1)
        self.assertEqual(
            [msg.formatted_message for msg in new_block.messages],
            ["新块消息"],
        )

        allow_first_callback_to_finish.set()
        await self._wait_for(lambda: len(callback_snapshots) == 2)

        self.assertEqual(callback_snapshots[0][1], ["旧块消息"])
        self.assertEqual(callback_snapshots[1][1], ["新块消息"])
        self.assertNotEqual(callback_snapshots[0][0], callback_snapshots[1][0])

    async def test_wait_timer_expiry_enters_quiet_window_before_processing(self) -> None:
        self._install_fake_judge(wait_seconds=0.03)
        callback_done = asyncio.Event()

        async def fake_reply(group_id: int, block: Any) -> None:
            _ = group_id, block
            callback_done.set()

        self.aggregator.set_reply_callback(fake_reply)

        await self._add_message("等待后再关块")
        await self._wait_for(
            lambda: self.aggregator._blocks[self.group_id].wait_task is not None
        )

        await asyncio.sleep(0.04)

        block = self.aggregator._blocks[self.group_id]
        self.assertFalse(block.is_processing)
        self.assertEqual(block.get_message_count(), 1)
        self.assertFalse(callback_done.is_set())

        await asyncio.wait_for(callback_done.wait(), timeout=0.5)

    async def test_wait_timer_blocks_on_format_tasks_before_quiet_window(self) -> None:
        self._install_fake_judge(wait_seconds=0.0)
        callback_done = asyncio.Event()
        allow_format_to_finish = asyncio.Event()

        async def fake_reply(group_id: int, block: Any) -> None:
            _ = group_id, block
            callback_done.set()

        async def slow_format_task() -> Any:
            await allow_format_to_finish.wait()
            return types.SimpleNamespace(
                formatted_message="<System-Message>格式化完成</System-Message>"
            )

        self.aggregator.set_reply_callback(fake_reply)

        await self._add_message(
            "慢格式化消息",
            formatted_message=None,
            format_task=asyncio.create_task(slow_format_task()),
        )

        await asyncio.sleep(0.08)
        self.assertFalse(callback_done.is_set())
        self.assertFalse(self.aggregator._blocks[self.group_id].is_processing)

        allow_format_to_finish.set()
        await asyncio.wait_for(callback_done.wait(), timeout=0.5)

    async def test_older_layer1_result_is_discarded_after_newer_same_block_judge_starts(self) -> None:
        judge_calls: list[tuple[int, int, list[str]]] = []
        discarded_versions: list[int] = []
        callback_snapshots: list[list[str]] = []
        callback_done = asyncio.Event()

        async def fake_judge(
            group_id: int,
            block: Any,
            expected_version: int,
            expected_judge_request_version: int,
        ) -> None:
            call_index = len(judge_calls) + 1
            judge_calls.append(
                (
                    expected_version,
                    expected_judge_request_version,
                    [msg.formatted_message for msg in block.messages],
                )
            )
            try:
                if call_index == 1:
                    await asyncio.sleep(0.08)
                else:
                    await asyncio.sleep(0)
            except asyncio.CancelledError:
                if call_index != 1:
                    raise

            lock = self.aggregator._get_lock(group_id)
            async with lock:
                if not self.aggregator._is_snapshot_current(
                    group_id,
                    block,
                    expected_version,
                    require_no_pending=True,
                    expected_judge_request_version=expected_judge_request_version,
                ):
                    discarded_versions.append(expected_judge_request_version)
                    return
                self.aggregator._schedule_wait_task(
                    group_id,
                    block,
                    expected_version,
                    0.0,
                    expected_judge_request_version,
                )

        async def fake_reply(group_id: int, block: Any) -> None:
            _ = group_id
            callback_snapshots.append(
                [msg.formatted_message for msg in block.messages]
            )
            callback_done.set()

        self.aggregator.set_reply_callback(fake_reply)
        self.aggregator._judge_wait_time = fake_judge

        await self._add_message("旧判断")
        await asyncio.sleep(0.01)
        await self._add_message("新判断")

        await asyncio.wait_for(callback_done.wait(), timeout=0.5)

        self.assertEqual(len(judge_calls), 2)
        self.assertEqual(judge_calls[0][2], ["旧判断"])
        self.assertEqual(judge_calls[1][2], ["旧判断", "新判断"])
        self.assertEqual(callback_snapshots, [["旧判断", "新判断"]])
        self.assertIn(judge_calls[0][1], discarded_versions)

    async def test_earliest_persisted_message_id_tracks_first_message_in_block(self) -> None:
        await self._add_message("第一条", user_id=1)
        await self._add_message("第二条", user_id=2)

        block = self.aggregator._blocks[self.group_id]
        self.assertEqual(block.get_earliest_persisted_message_id(), 1)


if __name__ == "__main__":
    unittest.main()

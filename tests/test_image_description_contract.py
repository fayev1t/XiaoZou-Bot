"""Contract tests for image_description（timeline 图片客观转录，2026-07-28）。

重点是**在途去重**：`agent_image_captions` 那层缓存只在调用结束落表之后才拦得住
重复，拦不住同一张新图被并发首次描述。没有 `_inflight` 的话，两条几乎同时到达
的消息（或同一条消息里重复贴的同一张图）会双双查空缓存、双双调 VLM，然后把两段
措辞不同的描述分别写进各自的事件正文——而事件 append-only，改不回来。

离线跑：`_load_cached` / `_invoke_vision` / `_store` 全部 patch 掉（真实现要
sqlalchemy + langchain + 配好的 vision 端点）。
"""

from __future__ import annotations

import asyncio
import unittest
from typing import Any
from unittest.mock import patch

from qqbot.services.agent_loop import image_description as mod

HASH_A = "a" * 64
HASH_B = "b" * 64
PNG = b"\x89PNG\r\n\x1a\nfake"


class _Harness:
    """把三个 IO 边界换成可控替身，并记下 VLM 被调了几次。"""

    def __init__(
        self,
        *,
        cached: str | None = None,
        answer: str | None = "一只猫",
        gate: asyncio.Event | None = None,
    ) -> None:
        self.cached = cached
        self.answer = answer
        self.gate = gate
        self.vision_calls: list[str] = []
        self.stored: list[dict[str, Any]] = []
        self._patches: list[Any] = []

    async def _load_cached(self, session_factory: Any, file_hash: str):
        return self.cached

    async def _invoke_vision(
        self, prompt: str, data: bytes, mime: str, file_hash: str, **kw: Any
    ):
        self.vision_calls.append(file_hash)
        if self.gate is not None:
            # 卡住第一个调用，制造"还没落表就来了第二个"的窗口
            await self.gate.wait()
        if self.answer is None:
            return None, None
        # 每次调用返回不同措辞：去重生效时两个协程必须拿到**同一个**结果
        return f"{self.answer}#{len(self.vision_calls)}", "fake/model"

    async def _store(self, session_factory: Any, **kwargs: Any) -> None:
        self.stored.append(kwargs)

    def __enter__(self) -> "_Harness":
        for name, impl in (
            ("_load_cached", self._load_cached),
            ("_invoke_vision", self._invoke_vision),
            ("_store", self._store),
            ("_load_prompt", lambda consumer: "PROMPT"),
        ):
            p = patch.object(mod, name, impl)
            p.start()
            self._patches.append(p)
        return self

    def __exit__(self, *exc: Any) -> None:
        for p in reversed(self._patches):
            p.stop()
        mod._inflight.clear()


def _describe(file_hash: str = HASH_A):
    return mod.describe_image(
        PNG, "image/png", file_hash, session_factory=object
    )


class InflightDedupTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_same_hash_calls_vlm_once(self) -> None:
        """同一张新图并发描述 → 只调一次 VLM，两个调用方拿到同一份描述。"""
        gate = asyncio.Event()
        with _Harness(gate=gate) as h:
            first = asyncio.ensure_future(_describe())
            await asyncio.sleep(0)  # 让 first 注册 inflight 并卡在 gate 上
            second = asyncio.ensure_future(_describe())
            await asyncio.sleep(0)
            gate.set()
            got = await asyncio.gather(first, second)

        self.assertEqual(len(h.vision_calls), 1)
        # 关键：两份描述必须逐字相同，否则同 hash 会在事件流里留下两个说法
        self.assertEqual(got[0], got[1])
        self.assertEqual(len(h.stored), 1)

    async def test_different_hashes_are_not_deduped(self) -> None:
        """去重按 hash，不同图各调各的。"""
        gate = asyncio.Event()
        with _Harness(gate=gate) as h:
            a = asyncio.ensure_future(_describe(HASH_A))
            b = asyncio.ensure_future(_describe(HASH_B))
            await asyncio.sleep(0)
            gate.set()
            await asyncio.gather(a, b)

        self.assertEqual(sorted(h.vision_calls), sorted([HASH_A, HASH_B]))

    async def test_inflight_entry_released_after_success(self) -> None:
        """登记必须摘干净，否则表会无界增长、且后续同图永远拿旧结果。"""
        with _Harness() as h:
            await _describe()
            self.assertEqual(mod._inflight, {})
            await _describe()  # 第二次是全新的一轮（本例 cached=None）
        self.assertEqual(len(h.vision_calls), 2)

    async def test_inflight_entry_released_after_failure(self) -> None:
        """VLM 返回空 → 结果 None，登记同样要摘掉（不能把失败钉死在表里）。"""
        with _Harness(answer=None) as h:
            self.assertIsNone(await _describe())
            self.assertEqual(mod._inflight, {})
        self.assertEqual(len(h.vision_calls), 1)

    async def test_waiter_gets_none_when_leader_raises(self) -> None:
        """本体抛异常（预料外）时等待者拿到 None 而不是永远挂着。

        describe_image 的降级语义是"没有描述"，等待者按同一套语义收场；异常
        本身照常上抛给发起者，由 media.py 的兜底 try/except 吞掉。
        """
        gate = asyncio.Event()

        async def boom(*args: Any, **kwargs: Any):
            await gate.wait()
            raise RuntimeError("unexpected")

        with _Harness(gate=gate):
            with patch.object(mod, "_describe_uncached", boom):
                first = asyncio.ensure_future(_describe())
                await asyncio.sleep(0)
                second = asyncio.ensure_future(_describe())
                await asyncio.sleep(0)
                gate.set()
                results = await asyncio.gather(first, second, return_exceptions=True)

        self.assertIsInstance(results[0], RuntimeError)
        self.assertIsNone(results[1])
        self.assertEqual(mod._inflight, {})

    async def test_cache_hit_skips_vision_entirely(self) -> None:
        """命中写时缓存 → 一次 VLM 都不调（重复表情包的常态路径）。"""
        with _Harness(cached="早就描述过了") as h:
            self.assertEqual(await _describe(), "早就描述过了")
        self.assertEqual(h.vision_calls, [])
        self.assertEqual(h.stored, [])


class ConcurrencyGateTests(unittest.TestCase):
    def test_semaphore_bound_is_five(self) -> None:
        """供应商单模型并发上限的那道闸。round_robin 只摊负载不限在飞数量，
        所以这道闸不能靠路由策略代替（LLM路由契约 §3）。"""
        self.assertEqual(mod._MAX_CONCURRENT_CALLS, 5)

    def test_look_path_shares_the_same_semaphore(self) -> None:
        """look_at_image 的重看与 ingest 描述共用一把信号量——换个入口不该
        绕过供应商的并发上限。"""
        import inspect

        source = inspect.getsource(mod.answer_about_image)
        self.assertIn("_semaphore", source)


if __name__ == "__main__":
    unittest.main()

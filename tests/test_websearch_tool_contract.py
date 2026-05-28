"""Contract tests for WebsearchTool (v2 重写).

Covers:
- 缺 SEARXNG_BASE_URL → RuntimeError
- query 为空 → ValueError
- happy path: SearXNG GET 调用形态（URL/参数）、返回结构（title/url/snippet）
- fetch_top_n=0 时不调 Crawl4AI
- fetch_top_n>0 但 CRAWL4AI_BASE_URL 缺 → warnings 含提示，正常 fall through
- fetch_top_n>0 时 Crawl4AI 调用形态（URL/JSON 体），抓取后填 fetched_text
- 单条抓取失败 → 该条 fetch_error 填充，整体仍返回
- max_results 截断、fetch_top_n 上限钳制
- Crawl4AI 响应多种包裹形态都能拎出文本（result/results/data.result/data.results/markdown 字典）

httpx 网络全部 mock：注入 _FakeHttpClient 工厂。
"""

from __future__ import annotations

import asyncio
import json
import os
import unittest
from typing import Any
from unittest import mock

from qqbot.services.agent_loop.tools.websearch import (
    WebsearchTool,
    _extract_crawl_text,
)


class _FakeResponse:
    def __init__(self, json_payload: Any, status_code: int = 200) -> None:
        self._json = json_payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if 400 <= self.status_code:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> Any:
        return self._json


class _FakeHttpClient:
    """支持 async with 的 mock client。按 (method, url) 路由响应。"""

    def __init__(self, responses: dict[tuple[str, str], Any]) -> None:
        # responses key: ("GET", url_prefix) or ("POST", url_prefix)
        # value: callable(params/json) -> _FakeResponse OR raises
        self._responses = responses
        self.calls: list[dict] = []

    async def __aenter__(self) -> "_FakeHttpClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def get(self, url: str, params: dict | None = None) -> _FakeResponse:
        self.calls.append({"method": "GET", "url": url, "params": params or {}})
        handler = self._match("GET", url)
        return handler({"params": params or {}})

    async def post(self, url: str, json: dict | None = None) -> _FakeResponse:
        self.calls.append({"method": "POST", "url": url, "json": json or {}})
        handler = self._match("POST", url)
        return handler({"json": json or {}})

    def _match(self, method: str, url: str):
        for (m, prefix), handler in self._responses.items():
            if m == method and url.startswith(prefix):
                return handler
        raise AssertionError(f"unexpected request {method} {url}")


def _patched_env(**vars: str) -> Any:
    return mock.patch.dict(os.environ, vars, clear=False)


class WebsearchToolContractTest(unittest.TestCase):
    def _build(
        self,
        client: _FakeHttpClient | None = None,
    ) -> WebsearchTool:
        return WebsearchTool(
            http_client_factory=(lambda: client) if client is not None else None,
        )

    def test_missing_searxng_base_url_raises(self) -> None:
        with _patched_env(SEARXNG_BASE_URL=""):
            tool = self._build(client=_FakeHttpClient({}))
            with self.assertRaises(RuntimeError):
                asyncio.run(tool.run({"query": "anything"}))

    def test_empty_query_raises(self) -> None:
        with _patched_env(SEARXNG_BASE_URL="http://sx:1"):
            tool = self._build(client=_FakeHttpClient({}))
            with self.assertRaises(ValueError):
                asyncio.run(tool.run({"query": "   "}))

    def test_searxng_happy_path_no_crawl(self) -> None:
        client = _FakeHttpClient(
            {
                ("GET", "http://sx:1/search"): lambda req: _FakeResponse(
                    {
                        "results": [
                            {
                                "title": "T1",
                                "url": "https://a/",
                                "content": "snippet 1",
                            },
                            {
                                "title": "T2",
                                "url": "https://b/",
                                "content": "snippet 2",
                            },
                        ]
                    }
                )
            }
        )
        with _patched_env(SEARXNG_BASE_URL="http://sx:1"):
            tool = self._build(client=client)
            result = asyncio.run(
                tool.run({"query": "rust async", "fetch_top_n": 0})
            )

        self.assertEqual(result["query"], "rust async")
        self.assertEqual(result["engine"], "searxng")
        self.assertEqual(len(result["results"]), 2)
        self.assertEqual(result["results"][0]["title"], "T1")
        self.assertEqual(result["results"][0]["url"], "https://a/")
        self.assertEqual(result["results"][0]["snippet"], "snippet 1")
        self.assertIsNone(result["results"][0]["fetched_text"])
        self.assertEqual(result["warnings"], [])
        # only one HTTP call (SearXNG GET)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0]["method"], "GET")
        self.assertEqual(client.calls[0]["params"].get("q"), "rust async")
        self.assertEqual(client.calls[0]["params"].get("format"), "json")

    def test_fetch_top_n_without_crawl_env_emits_warning(self) -> None:
        client = _FakeHttpClient(
            {
                ("GET", "http://sx:1/search"): lambda req: _FakeResponse(
                    {"results": [{"title": "T", "url": "https://a/", "content": ""}]}
                )
            }
        )
        with _patched_env(SEARXNG_BASE_URL="http://sx:1", CRAWL4AI_BASE_URL=""):
            tool = self._build(client=client)
            result = asyncio.run(
                tool.run({"query": "x", "fetch_top_n": 2})
            )
        self.assertEqual(len(client.calls), 1)
        self.assertTrue(any("CRAWL4AI_BASE_URL" in w for w in result["warnings"]))
        self.assertIsNone(result["results"][0]["fetched_text"])

    def test_fetch_top_n_with_crawl_happy_path(self) -> None:
        searxng = lambda req: _FakeResponse(
            {
                "results": [
                    {"title": "T1", "url": "https://a/", "content": "s1"},
                    {"title": "T2", "url": "https://b/", "content": "s2"},
                ]
            }
        )

        def crawl_handler(req: dict) -> _FakeResponse:
            payload = req["json"]
            urls = payload.get("urls") or []
            assert isinstance(urls, list) and len(urls) == 1
            url = urls[0]
            return _FakeResponse(
                {"result": {"markdown": f"BODY-of-{url}"}}
            )

        client = _FakeHttpClient(
            {
                ("GET", "http://sx:1/search"): searxng,
                ("POST", "http://cr:2/crawl"): crawl_handler,
            }
        )
        with _patched_env(
            SEARXNG_BASE_URL="http://sx:1", CRAWL4AI_BASE_URL="http://cr:2"
        ):
            tool = self._build(client=client)
            result = asyncio.run(tool.run({"query": "q", "fetch_top_n": 2}))

        self.assertEqual(
            result["results"][0]["fetched_text"], "BODY-of-https://a/"
        )
        self.assertEqual(
            result["results"][1]["fetched_text"], "BODY-of-https://b/"
        )
        # 1 GET + 2 POST
        self.assertEqual(len(client.calls), 3)
        post_calls = [c for c in client.calls if c["method"] == "POST"]
        self.assertEqual(len(post_calls), 2)
        # verify request envelope shape
        first_post = post_calls[0]
        self.assertIn("browser_config", first_post["json"])
        self.assertIn("crawler_config", first_post["json"])

    def test_per_url_crawl_failure_falls_through(self) -> None:
        def crawl_handler(req: dict) -> _FakeResponse:
            url = req["json"]["urls"][0]
            if "fail" in url:
                raise RuntimeError("crawl timeout")
            return _FakeResponse({"result": {"markdown": "OK"}})

        client = _FakeHttpClient(
            {
                ("GET", "http://sx:1/search"): lambda r: _FakeResponse(
                    {
                        "results": [
                            {"title": "T1", "url": "https://ok/", "content": ""},
                            {"title": "T2", "url": "https://fail/", "content": ""},
                        ]
                    }
                ),
                ("POST", "http://cr:2/crawl"): crawl_handler,
            }
        )
        with _patched_env(
            SEARXNG_BASE_URL="http://sx:1", CRAWL4AI_BASE_URL="http://cr:2"
        ):
            tool = self._build(client=client)
            result = asyncio.run(tool.run({"query": "q", "fetch_top_n": 2}))

        self.assertEqual(result["results"][0]["fetched_text"], "OK")
        self.assertIsNone(result["results"][1]["fetched_text"])
        self.assertIn("crawl timeout", result["results"][1]["fetch_error"])

    def test_max_results_truncated(self) -> None:
        client = _FakeHttpClient(
            {
                ("GET", "http://sx:1/search"): lambda r: _FakeResponse(
                    {
                        "results": [
                            {"title": f"T{i}", "url": f"https://x{i}/", "content": ""}
                            for i in range(20)
                        ]
                    }
                )
            }
        )
        with _patched_env(SEARXNG_BASE_URL="http://sx:1"):
            tool = self._build(client=client)
            result = asyncio.run(
                tool.run({"query": "q", "max_results": 3})
            )
        self.assertEqual(len(result["results"]), 3)

    def test_fetch_top_n_clamped_to_upper_bound(self) -> None:
        # arguments_schema says max 5; passing 100 should clamp.
        client = _FakeHttpClient(
            {
                ("GET", "http://sx:1/search"): lambda r: _FakeResponse(
                    {
                        "results": [
                            {"title": "T", "url": f"https://u{i}/", "content": ""}
                            for i in range(10)
                        ]
                    }
                ),
                ("POST", "http://cr:2/crawl"): lambda r: _FakeResponse(
                    {"result": {"markdown": "M"}}
                ),
            }
        )
        with _patched_env(
            SEARXNG_BASE_URL="http://sx:1", CRAWL4AI_BASE_URL="http://cr:2"
        ):
            tool = self._build(client=client)
            asyncio.run(tool.run({"query": "q", "fetch_top_n": 100}))
        # 1 GET + 5 POST (clamped to 5)
        posts = [c for c in client.calls if c["method"] == "POST"]
        self.assertEqual(len(posts), 5)


class ExtractCrawlTextContractTest(unittest.TestCase):
    """`_extract_crawl_text` 必须扛得住 Crawl4AI 多种响应包裹。"""

    def test_top_level_result(self) -> None:
        self.assertEqual(
            _extract_crawl_text({"result": {"markdown": "X"}}), "X"
        )

    def test_top_level_results_list(self) -> None:
        self.assertEqual(
            _extract_crawl_text(
                {"results": [{"cleaned_html": "Y"}, {"markdown": "Z"}]}
            ),
            "Y",
        )

    def test_data_wrapped_result(self) -> None:
        self.assertEqual(
            _extract_crawl_text(
                {"data": {"result": {"extracted_content": "C"}}}
            ),
            "C",
        )

    def test_data_wrapped_results_list(self) -> None:
        self.assertEqual(
            _extract_crawl_text({"data": {"results": [{"text": "T"}]}}),
            "T",
        )

    def test_markdown_nested_object(self) -> None:
        self.assertEqual(
            _extract_crawl_text(
                {"result": {"markdown": {"fit_markdown": "FM"}}}
            ),
            "FM",
        )

    def test_no_text_returns_empty(self) -> None:
        self.assertEqual(_extract_crawl_text({"result": {}}), "")
        self.assertEqual(_extract_crawl_text({}), "")
        self.assertEqual(_extract_crawl_text(None), "")


if __name__ == "__main__":
    unittest.main()

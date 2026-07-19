"""Contract tests for WebsearchTool (Exa 免 key 默认 / Tavily 可选, 2026-07-18 重做).

Covers:
- provider 分发：缺省/空 WEBSEARCH_PROVIDER → exa（无需任何 key）；tavily 需
  TAVILY_API_KEY（缺 → internal_tool_error）；非法 provider → internal_tool_error
- query 为空 → invalid_arguments（在 provider 判定之前）
- exa happy path: JSON-RPC 调用形态（method/params.name/arguments/Accept 头）、
  SSE 响应解析、"---" 分块 → title/url/snippet 映射、engine="exa"
- exa: JSON-RPC error / isError 结果 → internal_tool_error（BaseTool 兜底）
- exa: fetch_top_n>0 恒走进程内 GET 兜底抓正文
- tavily happy path: POST 体/Bearer 头、raw_content 命中直接填 fetched_text
  （8KB 截断）、缺失走兜底 GET、失败逐条吞进 fetch_error
- max_results 透传 + 截断、fetch_top_n 上限钳制
- _parse_exa_search_text 纯函数：分块/无 URL 丢弃/无 Highlights 退化/上限

httpx 网络全部 mock：注入 _FakeHttpClient 工厂。
"""

from __future__ import annotations

import asyncio
import json
import os
import unittest
from typing import Any
from unittest import mock

import httpx

from qqbot.services.agent_loop.tools.websearch import (
    WebsearchTool,
    _parse_exa_search_text,
)

_EXA = "https://mcp.exa.ai/mcp"
_TAVILY = "https://api.tavily.com/search"


def _ok(tool: WebsearchTool, args: dict) -> dict:
    """run() 返回 ToolOutcome；happy-path 统一取 .result 复用断言。"""
    outcome = asyncio.run(tool.run(args))
    assert outcome.ok, outcome
    return outcome.result


class _FakeResponse:
    def __init__(
        self,
        json_payload: Any = None,
        *,
        status_code: int = 200,
        text: str = "",
        content_type: str = "application/json",
        url: str = "",
    ) -> None:
        self._json = json_payload
        self.status_code = status_code
        self.text = text
        self.content = text.encode("utf-8")
        self.headers = {"content-type": content_type}
        self.url = url

    def raise_for_status(self) -> None:
        if 400 <= self.status_code:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> Any:
        return self._json


class _FakeHttpClient:
    """支持 async with 的 mock client。按 (method, url_prefix) 路由响应。"""

    def __init__(self, responses: dict[tuple[str, str], Any]) -> None:
        # responses value: callable(req) -> _FakeResponse OR raises
        self._responses = responses
        self.calls: list[dict] = []

    async def __aenter__(self) -> "_FakeHttpClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def get(
        self, url: str, params: dict | None = None, headers: dict | None = None
    ) -> _FakeResponse:
        self.calls.append(
            {"method": "GET", "url": url, "params": params or {}}
        )
        handler = self._match("GET", url)
        return handler({"url": url, "params": params or {}})

    async def post(
        self, url: str, json: dict | None = None, headers: dict | None = None
    ) -> _FakeResponse:
        self.calls.append(
            {
                "method": "POST",
                "url": url,
                "json": json or {},
                "headers": headers or {},
            }
        )
        handler = self._match("POST", url)
        return handler({"json": json or {}, "headers": headers or {}})

    def _match(self, method: str, url: str):
        for (m, prefix), handler in self._responses.items():
            if m == method and url.startswith(prefix):
                return handler
        raise AssertionError(f"unexpected request {method} {url}")


def _patched_env(**vars: str) -> Any:
    return mock.patch.dict(os.environ, vars, clear=False)


def _build(client: _FakeHttpClient | None = None) -> WebsearchTool:
    return WebsearchTool(
        http_client_factory=(lambda: client) if client is not None else None,
    )


def _sse(message: dict) -> str:
    """Exa MCP 的 SSE 响应包装。"""
    return "event: message\ndata: " + json.dumps(message) + "\n\n"


def _exa_result(text: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"content": [{"type": "text", "text": text}]},
    }


_EXA_TEXT_TWO_HITS = (
    "Title: T1\nURL: https://a/\nPublished: N/A\nAuthor: N/A\nHighlights:\n"
    "first line\n...\nsecond line\n"
    "\n---\n\n"
    "Title: T2\nURL: https://b/\nHighlights:\nsnippet two"
)


class WebsearchProviderDispatchTest(unittest.TestCase):
    def test_empty_query_returns_invalid_arguments(self) -> None:
        tool = _build(client=_FakeHttpClient({}))
        outcome = asyncio.run(tool.run({"query": "   "}))
        self.assertEqual(outcome.error_kind, "invalid_arguments")

    def test_unknown_provider_returns_internal_error(self) -> None:
        with _patched_env(WEBSEARCH_PROVIDER="bing"):
            tool = _build(client=_FakeHttpClient({}))
            outcome = asyncio.run(tool.run({"query": "q"}))
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "internal_tool_error")
        self.assertIn("WEBSEARCH_PROVIDER", outcome.error_message)

    def test_tavily_without_key_returns_internal_error(self) -> None:
        with _patched_env(WEBSEARCH_PROVIDER="tavily", TAVILY_API_KEY=""):
            tool = _build(client=_FakeHttpClient({}))
            outcome = asyncio.run(tool.run({"query": "q"}))
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "internal_tool_error")
        self.assertIn("TAVILY_API_KEY", outcome.error_message)


class WebsearchExaContractTest(unittest.TestCase):
    """默认后端：Exa 托管 MCP，免 key。"""

    def test_exa_happy_path_no_key_needed(self) -> None:
        client = _FakeHttpClient(
            {
                ("POST", _EXA): lambda req: _FakeResponse(
                    text=_sse(_exa_result(_EXA_TEXT_TWO_HITS)),
                    content_type="text/event-stream",
                )
            }
        )
        # WEBSEARCH_PROVIDER 缺省 + 无任何 key → 仍可用（免 key 是卖点）
        with _patched_env(WEBSEARCH_PROVIDER="", TAVILY_API_KEY=""):
            tool = _build(client=client)
            result = _ok(tool, {"query": "rust async", "fetch_top_n": 0})

        self.assertEqual(result["engine"], "exa")
        self.assertEqual(len(result["results"]), 2)
        self.assertEqual(result["results"][0]["title"], "T1")
        self.assertEqual(result["results"][0]["url"], "https://a/")
        self.assertEqual(result["results"][0]["snippet"], "first line second line")
        self.assertEqual(result["results"][1]["snippet"], "snippet two")
        self.assertIsNone(result["results"][0]["fetched_text"])
        self.assertEqual(result["warnings"], [])
        # 一次 JSON-RPC POST，形态符合 MCP tools/call 契约
        self.assertEqual(len(client.calls), 1)
        call = client.calls[0]
        self.assertEqual(call["json"]["method"], "tools/call")
        self.assertEqual(call["json"]["params"]["name"], "web_search_exa")
        self.assertEqual(
            call["json"]["params"]["arguments"],
            {"query": "rust async", "numResults": 10},
        )
        self.assertIn("text/event-stream", call["headers"]["Accept"])

    def test_exa_max_results_passed_through(self) -> None:
        client = _FakeHttpClient(
            {
                ("POST", _EXA): lambda req: _FakeResponse(
                    text=_sse(_exa_result(_EXA_TEXT_TWO_HITS)),
                    content_type="text/event-stream",
                )
            }
        )
        with _patched_env(WEBSEARCH_PROVIDER="exa"):
            tool = _build(client=client)
            result = _ok(tool, {"query": "q", "max_results": 1})
        self.assertEqual(
            client.calls[0]["json"]["params"]["arguments"]["numResults"], 1
        )
        self.assertEqual(len(result["results"]), 1)

    def test_exa_fetch_top_n_uses_inprocess_fetch(self) -> None:
        html = "<html><body><p>Page body here</p></body></html>"
        client = _FakeHttpClient(
            {
                ("POST", _EXA): lambda req: _FakeResponse(
                    text=_sse(_exa_result(_EXA_TEXT_TWO_HITS)),
                    content_type="text/event-stream",
                ),
                ("GET", "https://a/"): lambda req: _FakeResponse(
                    text=html, content_type="text/html", url="https://a/"
                ),
            }
        )
        with _patched_env(WEBSEARCH_PROVIDER="exa"):
            tool = _build(client=client)
            result = _ok(tool, {"query": "q", "fetch_top_n": 1})
        self.assertIn("Page body here", result["results"][0]["fetched_text"])
        self.assertIsNone(result["results"][1]["fetched_text"])
        # 1 POST + 1 GET（仅 top 1）
        self.assertEqual(len(client.calls), 2)

    def test_exa_jsonrpc_error_folds_to_internal_error(self) -> None:
        message = {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32000, "message": "rate limited"},
        }
        client = _FakeHttpClient(
            {
                ("POST", _EXA): lambda req: _FakeResponse(
                    text=_sse(message), content_type="text/event-stream"
                )
            }
        )
        with _patched_env(WEBSEARCH_PROVIDER="exa"):
            tool = _build(client=client)
            outcome = asyncio.run(tool.run({"query": "q"}))
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "internal_tool_error")
        self.assertIn("rate limited", outcome.error_message)

    def test_exa_is_error_result_folds_to_internal_error(self) -> None:
        message = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "isError": True,
                "content": [{"type": "text", "text": "upstream broke"}],
            },
        }
        client = _FakeHttpClient(
            {
                ("POST", _EXA): lambda req: _FakeResponse(
                    text=_sse(message), content_type="text/event-stream"
                )
            }
        )
        with _patched_env(WEBSEARCH_PROVIDER="exa"):
            tool = _build(client=client)
            outcome = asyncio.run(tool.run({"query": "q"}))
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "internal_tool_error")


class WebsearchTavilyContractTest(unittest.TestCase):
    """可选后端：WEBSEARCH_PROVIDER=tavily + TAVILY_API_KEY。"""

    def _env(self) -> Any:
        return _patched_env(
            WEBSEARCH_PROVIDER="tavily", TAVILY_API_KEY="tvly-test"
        )

    @staticmethod
    def _payload(items: list[dict]) -> dict:
        return {"query": "q", "results": items}

    def test_tavily_happy_path_no_fetch(self) -> None:
        client = _FakeHttpClient(
            {
                ("POST", _TAVILY): lambda req: _FakeResponse(
                    self._payload(
                        [
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
                    )
                )
            }
        )
        with self._env():
            tool = _build(client=client)
            result = _ok(tool, {"query": "rust async", "fetch_top_n": 0})

        self.assertEqual(result["query"], "rust async")
        self.assertEqual(result["engine"], "tavily")
        self.assertEqual(len(result["results"]), 2)
        self.assertEqual(result["results"][0]["title"], "T1")
        self.assertEqual(result["results"][0]["snippet"], "snippet 1")
        self.assertIsNone(result["results"][0]["fetched_text"])
        # 只有一次 HTTP 调用（Tavily POST），调用形态符合 API 契约
        self.assertEqual(len(client.calls), 1)
        call = client.calls[0]
        self.assertEqual(call["json"]["query"], "rust async")
        self.assertEqual(call["json"]["include_raw_content"], False)
        self.assertEqual(call["headers"]["Authorization"], "Bearer tvly-test")

    def test_raw_content_not_leaked_in_results(self) -> None:
        client = _FakeHttpClient(
            {
                ("POST", _TAVILY): lambda req: _FakeResponse(
                    self._payload(
                        [
                            {
                                "title": "T",
                                "url": "https://a/",
                                "content": "s",
                                "raw_content": "BODY",
                            }
                        ]
                    )
                )
            }
        )
        with self._env():
            tool = _build(client=client)
            result = _ok(tool, {"query": "q", "fetch_top_n": 1})
        self.assertNotIn("raw_content", result["results"][0])

    def test_fetch_top_n_uses_raw_content_without_extra_requests(self) -> None:
        long_body = "x" * 9000  # 超 8KB，验证截断
        client = _FakeHttpClient(
            {
                ("POST", _TAVILY): lambda req: _FakeResponse(
                    self._payload(
                        [
                            {
                                "title": "T1",
                                "url": "https://a/",
                                "content": "s1",
                                "raw_content": long_body,
                            },
                            {
                                "title": "T2",
                                "url": "https://b/",
                                "content": "s2",
                                "raw_content": "short body",
                            },
                        ]
                    )
                )
            }
        )
        with self._env():
            tool = _build(client=client)
            result = _ok(tool, {"query": "q", "fetch_top_n": 2})

        self.assertEqual(len(result["results"][0]["fetched_text"]), 8000)
        self.assertEqual(result["results"][1]["fetched_text"], "short body")
        # include_raw_content 打开、且没有任何兜底 GET
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0]["json"]["include_raw_content"], True)

    def test_fetch_top_n_falls_back_to_direct_fetch_when_raw_missing(
        self,
    ) -> None:
        html = (
            "<html><head><title>Doc</title><script>var x=1;</script></head>"
            "<body><p>Hello world</p></body></html>"
        )
        client = _FakeHttpClient(
            {
                ("POST", _TAVILY): lambda req: _FakeResponse(
                    self._payload(
                        [
                            {
                                "title": "T1",
                                "url": "https://a/",
                                "content": "s1",
                                "raw_content": None,
                            }
                        ]
                    )
                ),
                ("GET", "https://a/"): lambda req: _FakeResponse(
                    text=html, content_type="text/html", url="https://a/"
                ),
            }
        )
        with self._env():
            tool = _build(client=client)
            result = _ok(tool, {"query": "q", "fetch_top_n": 1})

        hit = result["results"][0]
        self.assertIn("Hello world", hit["fetched_text"])
        self.assertNotIn("var x=1", hit["fetched_text"])
        self.assertIsNone(hit["fetch_error"])
        # 1 POST + 1 兜底 GET
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(client.calls[1]["method"], "GET")

    def test_fallback_fetch_failure_fills_fetch_error(self) -> None:
        def failing_get(req: dict) -> _FakeResponse:
            raise httpx.ConnectError("net down")

        client = _FakeHttpClient(
            {
                ("POST", _TAVILY): lambda req: _FakeResponse(
                    self._payload(
                        [
                            {
                                "title": "T1",
                                "url": "https://ok/",
                                "content": "",
                                "raw_content": "BODY",
                            },
                            {
                                "title": "T2",
                                "url": "https://fail/",
                                "content": "",
                            },
                        ]
                    )
                ),
                ("GET", "https://fail/"): failing_get,
            }
        )
        with self._env():
            tool = _build(client=client)
            result = _ok(tool, {"query": "q", "fetch_top_n": 2})

        self.assertEqual(result["results"][0]["fetched_text"], "BODY")
        self.assertIsNone(result["results"][1]["fetched_text"])
        self.assertIn("ConnectError", result["results"][1]["fetch_error"])

    def test_max_results_passed_to_api_and_truncated(self) -> None:
        client = _FakeHttpClient(
            {
                ("POST", _TAVILY): lambda req: _FakeResponse(
                    self._payload(
                        [
                            {"title": f"T{i}", "url": f"https://x{i}/", "content": ""}
                            for i in range(20)
                        ]
                    )
                )
            }
        )
        with self._env():
            tool = _build(client=client)
            result = _ok(tool, {"query": "q", "max_results": 3})
        self.assertEqual(len(result["results"]), 3)
        self.assertEqual(client.calls[0]["json"]["max_results"], 3)

    def test_fetch_top_n_clamped_to_upper_bound(self) -> None:
        # arguments_schema says max 5; passing 100 should clamp.
        client = _FakeHttpClient(
            {
                ("POST", _TAVILY): lambda req: _FakeResponse(
                    self._payload(
                        [
                            {"title": "T", "url": f"https://u{i}/", "content": ""}
                            for i in range(10)
                        ]
                    )
                ),
                ("GET", "https://u"): lambda req: _FakeResponse(
                    text="body", content_type="text/plain", url=req["url"]
                ),
            }
        )
        with self._env():
            tool = _build(client=client)
            asyncio.run(tool.run({"query": "q", "fetch_top_n": 100}))
        # 1 POST + 5 兜底 GET（钳到 5）
        gets = [c for c in client.calls if c["method"] == "GET"]
        self.assertEqual(len(gets), 5)

    def test_non_dict_response_folds_to_internal_error(self) -> None:
        client = _FakeHttpClient(
            {("POST", _TAVILY): lambda req: _FakeResponse([1, 2, 3])}
        )
        with self._env():
            tool = _build(client=client)
            outcome = asyncio.run(tool.run({"query": "q"}))
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "internal_tool_error")


class ParseExaSearchTextContractTest(unittest.TestCase):
    """`_parse_exa_search_text` 必须扛得住 Exa 文本块的各种形态。"""

    def test_blocks_split_on_separator(self) -> None:
        hits = _parse_exa_search_text(_EXA_TEXT_TWO_HITS, 10)
        self.assertEqual([h["url"] for h in hits], ["https://a/", "https://b/"])

    def test_block_without_url_dropped(self) -> None:
        text = "Title: no url here\nHighlights:\nx\n\n---\n\nTitle: ok\nURL: https://a/"
        hits = _parse_exa_search_text(text, 10)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["url"], "https://a/")

    def test_missing_highlights_falls_back_to_body_lines(self) -> None:
        text = "Title: T\nURL: https://a/\nPublished: N/A\nsome loose line"
        hits = _parse_exa_search_text(text, 10)
        self.assertEqual(hits[0]["snippet"], "some loose line")

    def test_max_results_cap(self) -> None:
        text = "\n---\n".join(
            f"Title: T{i}\nURL: https://x{i}/" for i in range(10)
        )
        self.assertEqual(len(_parse_exa_search_text(text, 3)), 3)

    def test_empty_text_returns_no_hits(self) -> None:
        self.assertEqual(_parse_exa_search_text("", 10), [])


if __name__ == "__main__":
    unittest.main()

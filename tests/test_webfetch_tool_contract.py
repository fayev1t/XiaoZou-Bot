"""Contract tests for WebfetchTool (2026-07-18 新增) 与 _web_common 抓取层.

Covers:
- url 缺失 / 非 http(s) scheme / 环回与内网地址 → invalid_arguments
- happy path: GET 抓 HTML → title/text 提取（script/style 剥离、块级断行、
  实体解码）、url/final_url/status_code/content_type/truncated 字段
- max_chars 截断 + truncated 标记、下限钳制
- text/plain 直接透传（不走 HTML 提取）
- HTTP 404 / 网络错 / 非文本 content-type / 响应超 5MB → upstream_action_failed
- html_to_text 纯函数：嵌套 skip 子树、<br> 断行

httpx 网络全部 mock：注入 _FakeHttpClient 工厂（与 websearch 测试同构）。
"""

from __future__ import annotations

import asyncio
import unittest
from typing import Any

import httpx

from qqbot.services.agent_loop.tools._web_common import html_to_text
from qqbot.services.agent_loop.tools.webfetch import WebfetchTool


class _FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        text: str = "",
        content_type: str = "text/html",
        url: str = "",
    ) -> None:
        self.status_code = status_code
        self.text = text
        self.content = text.encode("utf-8")
        self.headers = {"content-type": content_type}
        self.url = url


class _FakeHttpClient:
    def __init__(self, handler: Any) -> None:
        # handler: callable(url) -> _FakeResponse OR raises
        self._handler = handler
        self.calls: list[str] = []

    async def __aenter__(self) -> "_FakeHttpClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append(url)
        return self._handler(url)


def _build(handler: Any) -> WebfetchTool:
    client = _FakeHttpClient(handler)
    tool = WebfetchTool(http_client_factory=lambda: client)
    return tool


def _run(tool: WebfetchTool, args: dict):
    return asyncio.run(tool.run(args))


_HTML = (
    "<html><head><title>My &amp; Page</title>"
    "<style>.x{color:red}</style><script>console.log(1)</script></head>"
    "<body><h1>Heading</h1><p>Hello world</p>"
    "<div>Second<br>line</div></body></html>"
)


class WebfetchArgumentGateTests(unittest.TestCase):
    def test_missing_url_returns_invalid_arguments(self) -> None:
        outcome = _run(_build(lambda url: _FakeResponse()), {})
        self.assertEqual(outcome.error_kind, "invalid_arguments")

    def test_non_http_scheme_rejected(self) -> None:
        for url in ("ftp://host/x", "file:///etc/passwd", "javascript:alert(1)"):
            with self.subTest(url=url):
                outcome = _run(_build(lambda u: _FakeResponse()), {"url": url})
                self.assertEqual(outcome.error_kind, "invalid_arguments")

    def test_loopback_and_private_hosts_rejected(self) -> None:
        for url in (
            "http://localhost/admin",
            "http://foo.localhost/",
            "http://127.0.0.1:8080/",
            "http://[::1]/",
            "http://10.0.0.5/",
            "http://192.168.1.1/",
            "http://169.254.169.254/latest/meta-data/",
        ):
            with self.subTest(url=url):
                tool = _build(lambda u: _FakeResponse())
                outcome = _run(tool, {"url": url})
                self.assertEqual(outcome.error_kind, "invalid_arguments")
        # 闸门在发请求之前——没有任何出站调用
        self.assertEqual(tool._client_factory().calls, [])

    def test_public_domain_allowed(self) -> None:
        outcome = _run(
            _build(
                lambda u: _FakeResponse(text="hi", content_type="text/plain")
            ),
            {"url": "https://example.com/"},
        )
        self.assertTrue(outcome.ok, outcome)


class WebfetchHappyPathTests(unittest.TestCase):
    def test_html_extraction_and_metadata(self) -> None:
        handler = lambda url: _FakeResponse(
            text=_HTML, content_type="text/html; charset=utf-8",
            url="https://site/page",
        )
        outcome = _run(_build(handler), {"url": "https://site/page"})
        self.assertTrue(outcome.ok, outcome)
        result = outcome.result
        self.assertEqual(result["url"], "https://site/page")
        self.assertEqual(result["final_url"], "https://site/page")
        self.assertEqual(result["status_code"], 200)
        self.assertEqual(result["content_type"], "text/html")
        self.assertEqual(result["title"], "My & Page")
        self.assertIn("Hello world", result["text"])
        self.assertIn("Heading", result["text"])
        self.assertNotIn("console.log", result["text"])
        self.assertNotIn("color:red", result["text"])
        self.assertFalse(result["truncated"])

    def test_plain_text_passthrough(self) -> None:
        handler = lambda url: _FakeResponse(
            text="  just plain text\n", content_type="text/plain"
        )
        outcome = _run(_build(handler), {"url": "https://site/robots.txt"})
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.result["text"], "just plain text")
        self.assertEqual(outcome.result["title"], "")

    def test_max_chars_truncation_and_clamp(self) -> None:
        handler = lambda url: _FakeResponse(
            text="x" * 1000, content_type="text/plain"
        )
        outcome = _run(
            _build(handler), {"url": "https://site/", "max_chars": 500}
        )
        self.assertEqual(len(outcome.result["text"]), 500)
        self.assertTrue(outcome.result["truncated"])
        # 低于下限的 max_chars 钳到 500
        outcome2 = _run(
            _build(handler), {"url": "https://site/", "max_chars": 1}
        )
        self.assertEqual(len(outcome2.result["text"]), 500)


class WebfetchUpstreamFailureTests(unittest.TestCase):
    def test_http_error_status(self) -> None:
        handler = lambda url: _FakeResponse(status_code=404, text="nope")
        outcome = _run(_build(handler), {"url": "https://site/missing"})
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "upstream_action_failed")
        self.assertIn("HTTP 404", outcome.error_message)

    def test_network_error_folds_to_upstream_failure(self) -> None:
        def handler(url: str) -> _FakeResponse:
            raise httpx.ConnectError("net down")

        outcome = _run(_build(handler), {"url": "https://site/"})
        self.assertEqual(outcome.error_kind, "upstream_action_failed")
        self.assertIn("ConnectError", outcome.error_message)

    def test_non_text_content_type_rejected(self) -> None:
        handler = lambda url: _FakeResponse(
            text="%PDF-1.7", content_type="application/pdf"
        )
        outcome = _run(_build(handler), {"url": "https://site/doc.pdf"})
        self.assertEqual(outcome.error_kind, "upstream_action_failed")
        self.assertIn("content type", outcome.error_message)

    def test_oversized_response_rejected(self) -> None:
        handler = lambda url: _FakeResponse(
            text="x" * (5 * 1024 * 1024 + 1), content_type="text/plain"
        )
        outcome = _run(_build(handler), {"url": "https://site/huge"})
        self.assertEqual(outcome.error_kind, "upstream_action_failed")
        self.assertIn("too large", outcome.error_message)


class HtmlToTextContractTest(unittest.TestCase):
    """html_to_text 纯函数的提取语义。"""

    def test_nested_skip_subtree(self) -> None:
        _, text = html_to_text(
            "<div>keep<script>a<b>c</b>d</script>also</div>"
        )
        self.assertIn("keep", text)
        self.assertIn("also", text)
        self.assertNotIn("a", text.replace("also", ""))

    def test_br_breaks_line(self) -> None:
        _, text = html_to_text("<p>one<br>two</p>")
        self.assertEqual(text.splitlines(), ["one", "two"])

    def test_blank_lines_collapsed(self) -> None:
        _, text = html_to_text(
            "<div><p>a</p></div><div></div><div><p>b</p></div>"
        )
        self.assertNotIn("\n\n\n", text)
        self.assertIn("a", text)
        self.assertIn("b", text)

    def test_title_extracted_even_with_head(self) -> None:
        title, _ = html_to_text("<head><title>  Hi   there </title></head>")
        self.assertEqual(title, "Hi there")


if __name__ == "__main__":
    unittest.main()

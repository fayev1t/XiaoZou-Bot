from __future__ import annotations

import importlib
import sys
import types
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


class FakeRequest:
    def __init__(self, method: str, url: str) -> None:
        self.method = method
        self.url = url


class FakeRequestError(Exception):
    def __init__(self, message: str, *, request: FakeRequest) -> None:
        super().__init__(message)
        self.request = request


class FakeConnectError(FakeRequestError):
    pass


class FakeHTTPStatusError(FakeRequestError):
    def __init__(self, response: FakeResponse) -> None:
        super().__init__(
            f"status_code={response.status_code}",
            request=response.request,
        )
        self.response = response


_MISSING_JSON = object()


class FakeResponse:
    def __init__(
        self,
        *,
        status_code: int,
        request: FakeRequest,
        json_payload: object = _MISSING_JSON,
        text: str = "",
    ) -> None:
        self.status_code = status_code
        self.request = request
        self._json_payload = json_payload
        self.text = text

    @property
    def is_error(self) -> bool:
        return self.status_code >= 400

    def json(self) -> object:
        if self._json_payload is _MISSING_JSON:
            raise ValueError("missing json payload")
        return self._json_payload

    def raise_for_status(self) -> None:
        if self.is_error:
            raise FakeHTTPStatusError(self)


class _DummyLogger:
    def info(self, *args: object, **kwargs: object) -> None:
        _ = args, kwargs

    def warning(self, *args: object, **kwargs: object) -> None:
        _ = args, kwargs


def _install_web_search_test_stubs() -> None:
    qqbot_package = types.ModuleType("qqbot")
    setattr(qqbot_package, "__path__", [str(ROOT / "qqbot")])
    sys.modules["qqbot"] = qqbot_package

    services_package = types.ModuleType("qqbot.services")
    setattr(services_package, "__path__", [str(ROOT / "qqbot" / "services")])
    sys.modules["qqbot.services"] = services_package

    core_package = types.ModuleType("qqbot.core")
    setattr(core_package, "__path__", [str(ROOT / "qqbot" / "core")])
    sys.modules["qqbot.core"] = core_package

    httpx_module = types.ModuleType("httpx")
    setattr(httpx_module, "AsyncClient", object)
    setattr(httpx_module, "Request", FakeRequest)
    setattr(httpx_module, "RequestError", FakeRequestError)
    setattr(httpx_module, "ConnectError", FakeConnectError)
    setattr(httpx_module, "ConnectTimeout", FakeConnectError)
    setattr(httpx_module, "HTTPStatusError", FakeHTTPStatusError)
    setattr(httpx_module, "Response", FakeResponse)
    sys.modules["httpx"] = httpx_module

    llm_module = types.ModuleType("qqbot.core.llm")

    async def create_llm(**kwargs: object) -> None:
        _ = kwargs
        return None

    setattr(llm_module, "create_llm", create_llm)
    sys.modules["qqbot.core.llm"] = llm_module

    logging_module = types.ModuleType("qqbot.core.logging")
    setattr(logging_module, "get_logger", lambda _name: _DummyLogger())
    sys.modules["qqbot.core.logging"] = logging_module

    settings_module = types.ModuleType("qqbot.core.settings")
    setattr(settings_module, "get_env_value", lambda key: None)
    sys.modules["qqbot.core.settings"] = settings_module

    langchain_core_package = types.ModuleType("langchain_core")
    setattr(langchain_core_package, "__path__", [])
    sys.modules["langchain_core"] = langchain_core_package

    messages_module = types.ModuleType("langchain_core.messages")

    @dataclass
    class _HumanMessage:
        content: str

    setattr(messages_module, "HumanMessage", _HumanMessage)
    sys.modules["langchain_core.messages"] = messages_module


def _load_web_search_module() -> Any:
    _install_web_search_test_stubs()
    sys.modules.pop("qqbot.services.web_search", None)
    return importlib.import_module("qqbot.services.web_search")


def _json_response(url: str, payload: object, *, status_code: int = 200) -> FakeResponse:
    return FakeResponse(
        status_code=status_code,
        json_payload=payload,
        request=FakeRequest("POST", url),
    )


def _text_response(url: str, text: str, *, status_code: int = 200) -> FakeResponse:
    return FakeResponse(
        status_code=status_code,
        text=text,
        request=FakeRequest("GET", url),
    )


class FakeAsyncClient:
    post_responses: list[FakeResponse | Exception] = []
    get_responses: list[FakeResponse | Exception] = []
    post_calls: list[tuple[str, dict[str, object]]] = []
    get_calls: list[str] = []

    def __init__(self, *, timeout: object, follow_redirects: bool) -> None:
        _ = timeout, follow_redirects

    async def __aenter__(self) -> FakeAsyncClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> None:
        _ = exc_type, exc, tb

    @classmethod
    def reset(cls) -> None:
        cls.post_responses = []
        cls.get_responses = []
        cls.post_calls = []
        cls.get_calls = []

    async def post(self, url: str, *, json: dict[str, object]) -> FakeResponse:
        type(self).post_calls.append((url, dict(json)))
        behavior = type(self).post_responses.pop(0)
        if isinstance(behavior, Exception):
            raise behavior
        return behavior

    async def get(self, url: str) -> FakeResponse:
        type(self).get_calls.append(url)
        behavior = type(self).get_responses.pop(0)
        if isinstance(behavior, Exception):
            raise behavior
        return behavior


class WebSearchServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        FakeAsyncClient.reset()
        self.module = _load_web_search_module()
        self.module.httpx.AsyncClient = FakeAsyncClient
        self.module.get_env_value = (
            lambda key: "http://crawl4ai.test" if key == "CRAWL4AI_BASE_URL" else None
        )
        self.service = self.module.WebSearchService()

    def assert_typed_crawl4ai_post_calls(self, urls: list[str]) -> None:
        self.assertEqual(
            FakeAsyncClient.post_calls,
            [
                (
                    "http://crawl4ai.test/crawl",
                    {
                        "urls": [url],
                        "browser_config": {"type": "BrowserConfig", "params": {"headless": True}},
                        "crawler_config": {"type": "CrawlerRunConfig", "params": {"stream": False}},
                    },
                )
                for url in urls
            ],
        )

    async def test_fetch_urls_uses_crawl4ai_http_and_parses_multiple_envelopes(self) -> None:
        FakeAsyncClient.post_responses = [
            _json_response(
                "http://crawl4ai.test/crawl",
                {"result": {"title": "标题A", "markdown": "<h1>A</h1> 内容"}},
            ),
            _json_response(
                "http://crawl4ai.test/crawl",
                {
                    "data": {
                        "results": [
                            {
                                "metadata": {"title": "标题B"},
                                "html": "<p>B</p> 内容",
                            }
                        ]
                    }
                },
            ),
        ]

        documents = await self.service._fetch_urls(["https://a.example", "https://b.example"])

        self.assert_typed_crawl4ai_post_calls(["https://a.example", "https://b.example"])
        self.assertEqual(FakeAsyncClient.get_calls, [])
        self.assertEqual(
            documents,
            [
                {
                    "url": "https://a.example",
                    "title": "标题A",
                    "content": "A 内容",
                },
                {
                    "url": "https://b.example",
                    "title": "标题B",
                    "content": "B 内容",
                },
            ],
        )

    async def test_fetch_urls_falls_back_to_httpx_when_service_is_unavailable(self) -> None:
        FakeAsyncClient.post_responses = [
            FakeConnectError(
                "crawl4ai down",
                request=FakeRequest("POST", "http://crawl4ai.test/crawl"),
            )
        ]
        FakeAsyncClient.get_responses = [
            _text_response("https://a.example", "<p>后备 A</p>"),
            _text_response("https://b.example", "<p>后备 B</p>"),
        ]

        documents = await self.service._fetch_urls(["https://a.example", "https://b.example"])

        self.assert_typed_crawl4ai_post_calls(["https://a.example"])
        self.assertEqual(FakeAsyncClient.get_calls, ["https://a.example", "https://b.example"])
        self.assertEqual(
            documents,
            [
                {"url": "https://a.example", "title": "", "content": "后备 A"},
                {"url": "https://b.example", "title": "", "content": "后备 B"},
            ],
        )

    async def test_fetch_urls_falls_back_only_for_unusable_service_payload(self) -> None:
        FakeAsyncClient.post_responses = [
            _json_response("http://crawl4ai.test/crawl", {"ok": True}),
            _json_response(
                "http://crawl4ai.test/crawl",
                {"data": {"result": {"title": "标题B", "markdown": "<div>服务 B</div>"}}},
            ),
        ]
        FakeAsyncClient.get_responses = [_text_response("https://a.example", "<div>后备 A</div>")]

        documents = await self.service._fetch_urls(["https://a.example", "https://b.example"])

        self.assert_typed_crawl4ai_post_calls(["https://a.example", "https://b.example"])
        self.assertEqual(FakeAsyncClient.get_calls, ["https://a.example"])
        self.assertEqual(
            documents,
            [
                {"url": "https://a.example", "title": "", "content": "后备 A"},
                {
                    "url": "https://b.example",
                    "title": "标题B",
                    "content": "服务 B",
                },
            ],
        )

    async def test_fetch_urls_falls_back_to_httpx_when_base_url_is_missing(self) -> None:
        self.module.get_env_value = lambda _key: None
        FakeAsyncClient.get_responses = [_text_response("https://a.example", "<div>后备 A</div>")]

        documents = await self.service._fetch_urls(["https://a.example"])

        self.assertEqual(FakeAsyncClient.post_calls, [])
        self.assertEqual(FakeAsyncClient.get_calls, ["https://a.example"])
        self.assertEqual(
            documents,
            [{"url": "https://a.example", "title": "", "content": "后备 A"}],
        )

    async def test_fetch_urls_falls_back_to_httpx_when_service_returns_5xx(self) -> None:
        FakeAsyncClient.post_responses = [
            _json_response(
                "http://crawl4ai.test/crawl",
                {"error": "server error"},
                status_code=503,
            )
        ]
        FakeAsyncClient.get_responses = [_text_response("https://a.example", "<div>后备 A</div>")]

        documents = await self.service._fetch_urls(["https://a.example"])

        self.assert_typed_crawl4ai_post_calls(["https://a.example"])
        self.assertEqual(FakeAsyncClient.get_calls, ["https://a.example"])
        self.assertEqual(
            documents,
            [{"url": "https://a.example", "title": "", "content": "后备 A"}],
        )

    async def test_fetch_urls_falls_back_to_httpx_when_service_returns_non_json(self) -> None:
        FakeAsyncClient.post_responses = [
            FakeResponse(
                status_code=200,
                request=FakeRequest("POST", "http://crawl4ai.test/crawl"),
                text="not json",
            )
        ]
        FakeAsyncClient.get_responses = [_text_response("https://a.example", "<div>后备 A</div>")]

        documents = await self.service._fetch_urls(["https://a.example"])

        self.assert_typed_crawl4ai_post_calls(["https://a.example"])
        self.assertEqual(FakeAsyncClient.get_calls, ["https://a.example"])
        self.assertEqual(
            documents,
            [{"url": "https://a.example", "title": "", "content": "后备 A"}],
        )

    async def test_fetch_single_url_uses_official_typed_payload(self) -> None:
        FakeAsyncClient.post_responses = [
            _json_response(
                "http://crawl4ai.test/crawl",
                {"result": {"title": "标题A", "markdown": "<p>服务 A</p>"}},
            ),
        ]

        document = await self.service._fetch_single_url_via_crawl4ai_http(
            client=FakeAsyncClient(timeout=20.0, follow_redirects=True),
            crawl_endpoint="http://crawl4ai.test/crawl",
            url="https://a.example",
        )

        self.assert_typed_crawl4ai_post_calls(["https://a.example"])
        self.assertEqual(
            document,
            {
                "url": "https://a.example",
                "title": "标题A",
                "content": "服务 A",
            },
        )

    async def test_payload_parser_prefers_known_envelopes_before_generic_scan(self) -> None:
        document = self.service._build_document_from_crawl4ai_payload(
            payload={
                "text": "外层包装文本",
                "result": {"title": "标题A", "markdown": "<p>服务 A</p>"},
            },
            requested_url="https://a.example",
        )

        self.assertEqual(
            document,
            {
                "url": "https://a.example",
                "title": "标题A",
                "content": "服务 A",
            },
        )


if __name__ == "__main__":
    unittest.main()

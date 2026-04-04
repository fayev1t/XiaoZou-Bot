from __future__ import annotations

import re

import httpx
from langchain_core.messages import HumanMessage

from qqbot.core.llm import create_llm
from qqbot.core.logging import get_logger
from qqbot.core.settings import get_env_value

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
_CRAWL4AI_CONTENT_KEYS = (
    "markdown",
    "cleaned_html",
    "html",
    "content",
    "text",
    "fit_markdown",
    "fit_html",
    "extracted_content",
)
logger = get_logger(__name__)


class _Crawl4AIServiceUnavailableError(RuntimeError):
    pass


class _Crawl4AIPayloadUnusableError(RuntimeError):
    pass


class WebSearchService:
    async def search(self, query: str) -> str:
        logger.info(
            "[web_search] search started",
            extra={"tool": "web_search", "input_preview": query[:120]},
        )
        documents = await self._search_and_fetch(query)
        summary = await self._summarize(
            tool_name="web_search",
            input_data=query,
            documents=documents,
        )
        logger.info(
            "[web_search] search finished",
            extra={
                "tool": "web_search",
                "input_preview": query[:120],
                "document_count": len(documents),
                "summary_length": len(summary),
            },
        )
        return summary

    async def crawl(self, url: str) -> str:
        logger.info(
            "[web_search] crawl started",
            extra={"tool": "web_crawl", "input_preview": url[:120]},
        )
        documents = await self._fetch_urls([url])
        summary = await self._summarize(
            tool_name="web_crawl",
            input_data=url,
            documents=documents,
        )
        logger.info(
            "[web_search] crawl finished",
            extra={
                "tool": "web_crawl",
                "input_preview": url[:120],
                "document_count": len(documents),
                "summary_length": len(summary),
            },
        )
        return summary

    async def _search_and_fetch(self, query: str) -> list[dict[str, str]]:
        urls = await self._search_urls(query)
        return await self._fetch_urls(urls)

    async def _search_urls(self, query: str) -> list[str]:
        base_url = get_env_value("SEARXNG_BASE_URL")
        if not base_url:
            logger.warning(
                "[web_search] search skipped because SEARXNG_BASE_URL is missing",
                extra={"tool": "web_search", "input_preview": query[:120]},
            )
            return []

        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            response = await client.get(
                f"{base_url.rstrip('/')}/search",
                params={
                    "q": query,
                    "format": "json",
                    "categories": "general",
                },
            )
            response.raise_for_status()
            payload = response.json()

        results = payload.get("results") if isinstance(payload, dict) else []
        urls: list[str] = []
        if not isinstance(results, list):
            return urls
        for item in results:
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                urls.append(url)
            if len(urls) >= 5:
                break
        logger.info(
            "[web_search] search provider returned urls",
            extra={
                "tool": "web_search",
                "input_preview": query[:120],
                "url_count": len(urls),
            },
        )
        return urls

    async def _fetch_urls(self, urls: list[str]) -> list[dict[str, str]]:
        normalized_urls = list(dict.fromkeys(url for url in urls if url))
        if not normalized_urls:
            logger.info("[web_search] no urls to fetch", extra={"url_count": 0})
            return []

        crawl4ai_base_url = get_env_value("CRAWL4AI_BASE_URL")
        if not crawl4ai_base_url:
            logger.warning(
                "[web_search] CRAWL4AI_BASE_URL missing, fallback to httpx",
                extra={"url_count": len(normalized_urls)},
            )
            return await self._fetch_urls_via_httpx(normalized_urls)

        crawl4ai_documents, fallback_urls = await self._fetch_urls_via_crawl4ai_http(
            normalized_urls,
            base_url=crawl4ai_base_url,
        )
        documents_by_url = {doc["url"]: doc for doc in crawl4ai_documents}

        if fallback_urls:
            fallback_documents = await self._fetch_urls_via_httpx(fallback_urls)
            documents_by_url.update({doc["url"]: doc for doc in fallback_documents})

        return [documents_by_url[url] for url in normalized_urls if url in documents_by_url]

    async def _fetch_urls_via_crawl4ai_http(
        self,
        urls: list[str],
        *,
        base_url: str,
    ) -> tuple[list[dict[str, str]], list[str]]:
        documents_by_url: dict[str, dict[str, str]] = {}
        fallback_urls: list[str] = []
        crawl_endpoint = f"{base_url.rstrip('/')}/crawl"

        async with httpx.AsyncClient(
            timeout=60.0,
            follow_redirects=True,
        ) as client:
            for index, url in enumerate(urls):
                try:
                    document = await self._fetch_single_url_via_crawl4ai_http(
                        client=client,
                        crawl_endpoint=crawl_endpoint,
                        url=url,
                    )
                except _Crawl4AIServiceUnavailableError as exc:
                    logger.warning(
                        "[web_search] crawl4ai service unavailable, fallback to httpx | url={} | error={}",
                        url,
                        str(exc),
                    )
                    fallback_urls.extend(urls[index:])
                    break
                except _Crawl4AIPayloadUnusableError as exc:
                    logger.warning(
                        "[web_search] crawl4ai payload unusable, fallback to httpx | url={} | detail={}",
                        url,
                        str(exc),
                    )
                    fallback_urls.append(url)
                    continue

                documents_by_url[url] = document

        documents = [documents_by_url[url] for url in urls if url in documents_by_url]
        logger.info(
            "[web_search] fetch via crawl4ai http finished | url_count={} | document_count={} | fallback_count={}",
            len(urls),
            len(documents),
            len(fallback_urls),
        )
        return documents, fallback_urls

    async def _fetch_single_url_via_crawl4ai_http(
        self,
        *,
        client: httpx.AsyncClient,
        crawl_endpoint: str,
        url: str,
    ) -> dict[str, str]:
        request_payload: dict[str, object] = {
            "urls": [url],
            "browser_config": {"type": "BrowserConfig", "params": {"headless": True}},
            "crawler_config": {"type": "CrawlerRunConfig", "params": {"stream": False}},
        }

        try:
            response = await client.post(crawl_endpoint, json=request_payload)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise _Crawl4AIServiceUnavailableError(str(exc)) from exc
        except httpx.RequestError as exc:
            raise _Crawl4AIPayloadUnusableError(
                f"request_error={type(exc).__name__}: {exc}",
            ) from exc

        if response.status_code >= 500:
            raise _Crawl4AIServiceUnavailableError(
                self._describe_crawl4ai_http_response(response),
            )

        if response.is_error:
            raise _Crawl4AIPayloadUnusableError(
                self._describe_crawl4ai_http_response(response),
            )

        try:
            payload = response.json()
        except ValueError:
            raise _Crawl4AIPayloadUnusableError(
                self._describe_crawl4ai_http_response(response),
            )

        document = self._build_document_from_crawl4ai_payload(
            payload=payload,
            requested_url=url,
        )
        if document is None:
            raise _Crawl4AIPayloadUnusableError(
                self._describe_crawl4ai_payload(payload),
            )

        return document

    @staticmethod
    def _describe_crawl4ai_http_response(response: httpx.Response) -> str:
        headers = getattr(response, "headers", {})
        if isinstance(headers, dict):
            content_type = str(headers.get("content-type", ""))
        else:
            content_type = str(getattr(headers, "get", lambda *_args, **_kwargs: "")("content-type", ""))

        body_preview = _WHITESPACE_RE.sub(" ", getattr(response, "text", "")).strip()
        if len(body_preview) > 240:
            body_preview = f"{body_preview[:240]}..."

        return (
            f"status_code={response.status_code}, "
            f"content_type={content_type or 'unknown'}, "
            f"body_preview={body_preview or '<empty>'}"
        )

    @staticmethod
    def _describe_crawl4ai_payload(payload: object) -> str:
        if isinstance(payload, dict):
            top_level_keys = ", ".join(sorted(str(key) for key in payload.keys())[:12])
            return f"payload_type=dict, top_level_keys={top_level_keys or '<none>'}"

        if isinstance(payload, list):
            return f"payload_type=list, item_count={len(payload)}"

        return f"payload_type={type(payload).__name__}"

    def _build_document_from_crawl4ai_payload(
        self,
        *,
        payload: object,
        requested_url: str,
    ) -> dict[str, str] | None:
        for candidate in self._iter_preferred_crawl4ai_candidates(payload):
            content = self._extract_crawl4ai_content(candidate)
            if content is None:
                continue

            normalized = self._normalize_content(content)
            if not normalized:
                continue

            return {
                "url": self._extract_crawl4ai_url(candidate, requested_url),
                "title": self._extract_crawl4ai_title(candidate),
                "content": normalized,
            }

        return None

    def _iter_preferred_crawl4ai_candidates(self, payload: object) -> list[dict[str, object]]:
        candidates: list[dict[str, object]] = []
        seen_ids: set[int] = set()

        def add_candidate(candidate: object) -> None:
            if isinstance(candidate, dict):
                candidate_id = id(candidate)
                if candidate_id not in seen_ids:
                    seen_ids.add(candidate_id)
                    candidates.append(candidate)

        if isinstance(payload, dict):
            add_candidate(payload.get("result"))

            results = payload.get("results")
            if isinstance(results, list):
                for item in results:
                    add_candidate(item)

            data = payload.get("data")
            if isinstance(data, dict):
                add_candidate(data.get("result"))

                data_results = data.get("results")
                if isinstance(data_results, list):
                    for item in data_results:
                        add_candidate(item)

        for candidate in self._iter_crawl4ai_candidates(payload):
            add_candidate(candidate)

        return candidates

    def _iter_crawl4ai_candidates(self, payload: object) -> list[dict[str, object]]:
        pending = [payload]
        candidates: list[dict[str, object]] = []
        seen_ids: set[int] = set()

        while pending:
            current = pending.pop(0)
            current_id = id(current)
            if current_id in seen_ids:
                continue
            seen_ids.add(current_id)

            if isinstance(current, dict):
                candidates.append(current)
                pending.extend(
                    value for value in current.values() if isinstance(value, (dict, list))
                )
                continue

            if isinstance(current, list):
                pending.extend(item for item in current if isinstance(item, (dict, list)))

        return candidates

    @staticmethod
    def _extract_crawl4ai_content(candidate: dict[str, object]) -> str | None:
        for key in _CRAWL4AI_CONTENT_KEYS:
            value = candidate.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return None

    @staticmethod
    def _extract_crawl4ai_url(candidate: dict[str, object], requested_url: str) -> str:
        url = candidate.get("url")
        if isinstance(url, str) and url.strip():
            return url

        metadata = candidate.get("metadata")
        if isinstance(metadata, dict):
            metadata_url = metadata.get("url")
            if isinstance(metadata_url, str) and metadata_url.strip():
                return metadata_url

        return requested_url

    @staticmethod
    def _extract_crawl4ai_title(candidate: dict[str, object]) -> str:
        title = candidate.get("title") or candidate.get("page_title")
        if isinstance(title, str):
            return title

        metadata = candidate.get("metadata")
        if isinstance(metadata, dict):
            metadata_title = metadata.get("title")
            if isinstance(metadata_title, str):
                return metadata_title

        return ""

    async def _fetch_urls_via_httpx(self, urls: list[str]) -> list[dict[str, str]]:
        documents: list[dict[str, str]] = []
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            for url in urls:
                try:
                    response = await client.get(url)
                    response.raise_for_status()
                except Exception as exc:
                    logger.warning(
                        "[web_search] httpx fetch failed | url={} | error={}",
                        url,
                        str(exc),
                    )
                    continue
                documents.append(
                    {
                        "url": url,
                        "title": "",
                        "content": self._normalize_content(response.text),
                    }
                )
        logger.info(
            "[web_search] fetch via httpx finished | url_count={} | document_count={}",
            len(urls),
            len(documents),
        )
        return documents

    async def _summarize(
        self,
        *,
        tool_name: str,
        input_data: str,
        documents: list[dict[str, str]],
    ) -> str:
        if not documents:
            return "未获取到可用网页结果。"

        llm = await create_llm(temperature=0.2)
        source_lines = [
            f"来源 {index + 1}：{doc['url']}\n标题：{doc['title'] or '无'}\n内容：{doc['content']}"
            for index, doc in enumerate(documents[:5])
        ]
        merged_sources = "\n\n".join(source_lines)

        if llm is None:
            return merged_sources[:4000]

        prompt = (
            "你是网页工具结果总结器。请把下面抓取到的网页内容压缩成一段可复用、"
            "适合直接放入群聊上下文的中文摘要。\n"
            f"工具：{tool_name}\n输入：{input_data}\n\n"
            "要求：\n"
            "1. 保留关键事实与结论。\n"
            "2. 尽量提到来源 URL。\n"
            "3. 不要输出多余前言。\n\n"
            f"{merged_sources}"
        )
        response = await llm.ainvoke([HumanMessage(content=prompt)])
        if isinstance(response.content, str) and response.content.strip():
            return response.content.strip()
        return merged_sources[:4000]

    @staticmethod
    def _normalize_content(content: str) -> str:
        text = _HTML_TAG_RE.sub(" ", content)
        text = _WHITESPACE_RE.sub(" ", text).strip()
        return text[:6000]

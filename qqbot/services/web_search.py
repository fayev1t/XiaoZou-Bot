from __future__ import annotations

import re

import httpx
from langchain_core.messages import HumanMessage

from qqbot.core.llm import create_llm
from qqbot.core.logging import get_logger
from qqbot.core.settings import get_env_value

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
logger = get_logger(__name__)


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

        try:
            from crawl4ai import AsyncWebCrawler  # type: ignore
        except ImportError:
            logger.warning(
                "[web_search] crawl4ai unavailable, fallback to httpx",
                extra={"url_count": len(normalized_urls)},
            )
            return await self._fetch_urls_via_httpx(normalized_urls)

        documents: list[dict[str, str]] = []
        async with AsyncWebCrawler() as crawler:
            results = await crawler.arun_many(normalized_urls)
        for result in results:
            url = getattr(result, "url", "") or ""
            content = (
                getattr(result, "markdown", "")
                or getattr(result, "cleaned_html", "")
                or getattr(result, "html", "")
                or ""
            )
            title = getattr(result, "title", "") or ""
            normalized = self._normalize_content(content)
            if normalized:
                documents.append({"url": url, "title": title, "content": normalized})
        logger.info(
            "[web_search] fetch via crawl4ai finished",
            extra={"url_count": len(normalized_urls), "document_count": len(documents)},
        )
        return documents

    async def _fetch_urls_via_httpx(self, urls: list[str]) -> list[dict[str, str]]:
        documents: list[dict[str, str]] = []
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            for url in urls:
                try:
                    response = await client.get(url)
                    response.raise_for_status()
                except Exception as exc:
                    logger.warning(
                        "[web_search] httpx fetch failed",
                        extra={"url": url, "error": str(exc)},
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
            "[web_search] fetch via httpx finished",
            extra={"url_count": len(urls), "document_count": len(documents)},
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

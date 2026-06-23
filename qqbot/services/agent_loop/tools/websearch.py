"""WebsearchTool — v2 自带的网页搜索工具。

接两个外部服务（部署在 docker 里）：
  SEARXNG_BASE_URL    元搜索引擎，返回标题/URL/snippet
  CRAWL4AI_BASE_URL   抓取选定 URL 的正文（可选）

参数：
  query        必填，搜索关键词
  fetch_top_n  可选，默认 0；>0 时对前 N 条结果用 Crawl4AI 抓取正文塞入 fetched_text

返回：
  {
    "query": str,
    "engine": "searxng",
    "results": [{
        "title": str,
        "url": str,
        "snippet": str,
        "fetched_text": str | None,     # 仅 fetch_top_n>0 命中范围内填充
        "fetch_error": str | None,      # 若该 URL 抓取失败
    }, ...],
    "warnings": [str, ...]              # SearXNG/Crawl4AI 部分失败的提示
  }

错误策略：
  - 配置缺失 (SEARXNG_BASE_URL 缺) → raise RuntimeError；ToolWorker 写
    agent.tool_failed
  - SearXNG HTTP 错 / 解析错 → raise，统一上层处理
  - Crawl4AI 抓取单条失败 → 不抛，吞到 fetch_error 字段；保持整体可用
  - 网络超时 → raise（让 LLM 重试或换方向）

不复用 v1 qqbot/services/web_search.py：v2 重写。仅复用 env 变量名
（SEARXNG_BASE_URL / CRAWL4AI_BASE_URL）作为部署侧约定。
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from qqbot.core.logging import get_logger
from qqbot.services.agent_loop.prompts import load_sibling_md
from qqbot.services.agent_loop.tool_registry import BaseTool

logger = get_logger(__name__)

_DEFAULT_TIMEOUT_SEC = 10.0
_DEFAULT_MAX_RESULTS = 10
_MAX_FETCH_TOP_N = 5  # 单次调用最多对前 N 条抓正文，防爆

# 模块加载期一次性读 sibling .md；缺失时 usage_prompt 为空串，PromptRegistry
# 渲染时会跳过该 section 而不是注入空标题。
_USAGE_PROMPT = load_sibling_md(__file__, "websearch.md")


class WebsearchTool(BaseTool):
    name = "websearch"
    description = (
        "Search the web via a SearXNG meta-search and optionally fetch the "
        "full body of the top results via Crawl4AI. Use this when you need "
        "up-to-date factual information beyond your training data."
    )
    usage_prompt = _USAGE_PROMPT
    # required_permission / require_bot_admin 用 BaseTool 默认值（GUEST /
    # False）：任意人都能让小奏查资料，小奏自己不需要是管理员。
    arguments_schema = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search keywords. Plain natural language.",
            },
            "fetch_top_n": {
                "type": "integer",
                "minimum": 0,
                "maximum": _MAX_FETCH_TOP_N,
                "default": 0,
                "description": (
                    "If > 0, fetch the full body text of the top N results via "
                    "Crawl4AI. Costly — only ask for content when the snippet "
                    "is insufficient."
                ),
            },
            "max_results": {
                "type": "integer",
                "minimum": 1,
                "maximum": 20,
                "default": _DEFAULT_MAX_RESULTS,
                "description": "Upper bound on number of search hits returned.",
            },
        },
        "required": ["query"],
    }

    def __init__(
        self,
        http_client_factory: Any | None = None,
        timeout_sec: float = _DEFAULT_TIMEOUT_SEC,
    ) -> None:
        # 注入点便于测试：传入一个无参 callable，返回一个支持 async with
        # 的 httpx.AsyncClient 兼容对象。
        self._client_factory = http_client_factory or (
            lambda: httpx.AsyncClient(timeout=timeout_sec, follow_redirects=True)
        )

    async def run(self, arguments: dict, **_: Any) -> dict:
        # **_ 兼容 Tool 协议的 context kwargs（scope_key 等）；websearch 不需要。
        query = (arguments.get("query") or "").strip()
        if not query:
            raise ValueError("query is required and must be non-empty")
        fetch_top_n = _clamp_int(
            arguments.get("fetch_top_n", 0), 0, _MAX_FETCH_TOP_N
        )
        max_results = _clamp_int(
            arguments.get("max_results", _DEFAULT_MAX_RESULTS), 1, 20
        )

        searxng_base = (os.getenv("SEARXNG_BASE_URL") or "").strip()
        if not searxng_base:
            raise RuntimeError("SEARXNG_BASE_URL is not configured")

        warnings: list[str] = []

        async with self._client_factory() as client:
            hits = await self._search(client, searxng_base, query, max_results)

            if fetch_top_n > 0:
                crawl_base = (os.getenv("CRAWL4AI_BASE_URL") or "").strip()
                if not crawl_base:
                    warnings.append(
                        "fetch_top_n requested but CRAWL4AI_BASE_URL not configured"
                    )
                else:
                    for hit in hits[:fetch_top_n]:
                        try:
                            text = await self._fetch(client, crawl_base, hit["url"])
                            hit["fetched_text"] = text
                        except Exception as exc:
                            hit["fetched_text"] = None
                            hit["fetch_error"] = f"{type(exc).__name__}: {exc}"

        return {
            "query": query,
            "engine": "searxng",
            "results": hits,
            "warnings": warnings,
        }

    async def _search(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        query: str,
        max_results: int,
    ) -> list[dict]:
        endpoint = f"{base_url.rstrip('/')}/search"
        params = {
            "q": query,
            "format": "json",
            "language": "zh-CN",
            "safesearch": "0",
        }
        response = await client.get(endpoint, params=params)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError(
                f"unexpected SearXNG response shape: {type(payload).__name__}"
            )
        raw = payload.get("results")
        if not isinstance(raw, list):
            return []
        hits: list[dict] = []
        for item in raw[:max_results]:
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            if not isinstance(url, str) or not url:
                continue
            hits.append(
                {
                    "title": str(item.get("title") or "")[:200],
                    "url": url,
                    "snippet": str(item.get("content") or "")[:1000],
                    "fetched_text": None,
                    "fetch_error": None,
                }
            )
        return hits

    async def _fetch(
        self, client: httpx.AsyncClient, base_url: str, url: str
    ) -> str:
        """调 Crawl4AI 抓正文。响应包裹形态有多种：尽量找到 markdown / cleaned_html / extracted_content。"""
        endpoint = f"{base_url.rstrip('/')}/crawl"
        request_payload = {
            "urls": [url],
            "browser_config": {
                "type": "BrowserConfig",
                "params": {"headless": True},
            },
            "crawler_config": {
                "type": "CrawlerRunConfig",
                "params": {"stream": False},
            },
        }
        response = await client.post(endpoint, json=request_payload)
        response.raise_for_status()
        payload = response.json()
        text = _extract_crawl_text(payload)
        if not text:
            raise RuntimeError("crawl4ai returned no text content")
        # 截断保护：单条最多 8KB 文本，避免一次塞爆 LLM context。
        return text[:8000]


def _extract_crawl_text(payload: Any) -> str:
    """从 Crawl4AI 多种响应包裹中拎出文本。
    可能的形态：{"result": {...}}, {"results": [{...}]}, {"data": {...}}, 直接对象。
    每一项里再找 markdown / cleaned_html / extracted_content / text 字段。
    """
    candidates: list[dict] = []

    def _add(obj: Any) -> None:
        if isinstance(obj, dict):
            candidates.append(obj)
        elif isinstance(obj, list):
            for x in obj:
                if isinstance(x, dict):
                    candidates.append(x)

    if isinstance(payload, dict):
        _add(payload.get("result"))
        _add(payload.get("results"))
        data = payload.get("data")
        if isinstance(data, dict):
            _add(data.get("result"))
            _add(data.get("results"))
        if not candidates:
            _add(payload)
    else:
        _add(payload)

    for candidate in candidates:
        for key in ("markdown", "cleaned_html", "extracted_content", "text"):
            value = candidate.get(key)
            if isinstance(value, str) and value.strip():
                return value
        # markdown 字段在新版可能是嵌套 dict (raw_markdown / fit_markdown)
        md_obj = candidate.get("markdown")
        if isinstance(md_obj, dict):
            for key in ("fit_markdown", "raw_markdown", "markdown_with_citations"):
                value = md_obj.get(key)
                if isinstance(value, str) and value.strip():
                    return value
    return ""


def _clamp_int(value: Any, lo: int, hi: int) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return lo
    return max(lo, min(hi, v))

"""WebsearchTool — v2 网页搜索工具（Exa 免 key 默认 / Tavily 可选）。

2026-07-18 重做：后端从自部署 SearXNG + Crawl4AI（两个 docker 服务，后者
还要跑 headless 浏览器）切换为外部搜索 API，两个容器全部下线。业界同类
（Claude Code / Codex / OpenCode）的 search 层一律外接搜索服务而非自建，
本次对齐。同日应用户要求把默认后端定为免 key 的 Exa。

后端由 env `WEBSEARCH_PROVIDER` 选择（缺省 exa）：
  exa      默认。Exa 托管 MCP（https://mcp.exa.ai/mcp，OpenCode websearch
           同款）：**免 key、无 MCP 握手**，直接 POST 一条 JSON-RPC
           tools/call(web_search_exa)，响应为 SSE 包装的 JSON，结果在单个
           text 块里以 "\\n---\\n" 分隔、每块 Title:/URL:/Highlights: 结构。
           属官方公开但无 SLA 承诺的免费口，失效时可切 tavily。
  tavily   备选。Tavily Search API（一次 HTTPS POST，需 env
           `TAVILY_API_KEY`）；include_raw_content 可随响应带回正文。

参数（对外形态与 SearXNG 版完全一致，planner 侧无感）：
  query        必填，搜索关键词
  fetch_top_n  可选，默认 0；>0 时给前 N 条结果附带正文 fetched_text
  max_results  可选，默认 10，上限 20

正文降级链（fetch_top_n>0 时逐条）：
  ① 搜索后端随响应带回的正文（仅 tavily 的 raw_content）命中即用；
  ② 否则进程内 httpx 直接抓（_web_common.fetch_page，stdlib HTML→纯文本，
     无浏览器）——exa 路径恒走这条。

返回：
  {
    "query": str,
    "engine": "exa" | "tavily",
    "results": [{
        "title": str,
        "url": str,
        "snippet": str,
        "fetched_text": str | None,     # 仅 fetch_top_n>0 命中范围内填充
        "fetch_error": str | None,      # 若该 URL 正文拿不到
    }, ...],
    "warnings": [str, ...]              # 保留字段（形态兼容），当前恒空
  }

错误策略（统一结构化 ToolOutcome，全程无 raise 控制流，见契约 §7.2）：
  - query 为空 → return ToolOutcome.failure(invalid_arguments)
  - 配置错（WEBSEARCH_PROVIDER 非法 / tavily 缺 TAVILY_API_KEY）→
    return ToolOutcome.failure(internal_tool_error)
  - 上游响应形态异常 / JSON-RPC error / HTTP 错 / 网络超时 → 普通异常上抛
    → BaseTool.run 兜底 internal_tool_error
  - 单条正文兜底抓取失败 → 不抛，吞到 fetch_error 字段；保持整体可用
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

from qqbot.core.logging import get_logger
from qqbot.services.agent_loop.prompts import load_sibling_md
from qqbot.services.agent_loop.tool_registry import BaseTool, ToolOutcome
from qqbot.services.agent_loop.tools._web_common import clamp_int, fetch_page

logger = get_logger(__name__)

_EXA_MCP_ENDPOINT = "https://mcp.exa.ai/mcp"
_TAVILY_ENDPOINT = "https://api.tavily.com/search"
_DEFAULT_TIMEOUT_SEC = 20.0
_DEFAULT_MAX_RESULTS = 10
_MAX_FETCH_TOP_N = 5  # 单次调用最多给前 N 条附正文，防爆
_MAX_FETCHED_TEXT_CHARS = 8000  # 单条正文上限，避免一次塞爆 LLM context

# 模块加载期一次性读 sibling .md；缺失时 usage_prompt 为空串，PromptRegistry
# 渲染时会跳过该 section 而不是注入空标题。
_USAGE_PROMPT = load_sibling_md(__file__, "websearch.md")


class WebsearchTool(BaseTool):
    name = "websearch"
    description = (
        "Search the web and optionally include the full body text of the top "
        "results. Use this when you need up-to-date factual information "
        "beyond your training data."
    )
    usage_prompt = _USAGE_PROMPT
    # required_permission / required_bot_role 用 BaseTool 默认值（GUEST /
    # 不限 bot 角色）：任意人都能让小奏查资料，小奏自己不需要是管理员。
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
                    "If > 0, also return the full body text of the top N "
                    "results. Costly — only ask for content when the snippet "
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

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        # GUEST + 不限 scope：enforce_access 实为 no-op，但统一保留首行调用。全程无 raise。
        if fail := await self.enforce_access(context):
            return fail

        query = (arguments.get("query") or "").strip()
        if not query:
            return ToolOutcome.failure(
                "invalid_arguments", "query is required and must be non-empty"
            )
        fetch_top_n = clamp_int(
            arguments.get("fetch_top_n", 0), 0, _MAX_FETCH_TOP_N
        )
        max_results = clamp_int(
            arguments.get("max_results", _DEFAULT_MAX_RESULTS), 1, 20
        )

        provider = (
            (os.getenv("WEBSEARCH_PROVIDER") or "").strip().lower() or "exa"
        )
        if provider not in ("exa", "tavily"):
            # 部署侧配置手滑 —— 非用户参数问题，归 internal。
            return ToolOutcome.failure(
                "internal_tool_error",
                f"unknown WEBSEARCH_PROVIDER: {provider!r} "
                "(expected 'exa' or 'tavily')",
            )
        api_key = (os.getenv("TAVILY_API_KEY") or "").strip()
        if provider == "tavily" and not api_key:
            return ToolOutcome.failure(
                "internal_tool_error",
                "WEBSEARCH_PROVIDER=tavily but TAVILY_API_KEY is not configured",
            )

        warnings: list[str] = []

        async with self._client_factory() as client:
            if provider == "exa":
                hits = await self._search_exa(client, query, max_results)
            else:
                hits = await self._search_tavily(
                    client,
                    api_key,
                    query,
                    max_results,
                    include_raw_content=fetch_top_n > 0,
                )

            for index, hit in enumerate(hits):
                raw = hit.pop("raw_content", None)
                if index >= fetch_top_n:
                    continue
                if isinstance(raw, str) and raw.strip():
                    hit["fetched_text"] = raw.strip()[:_MAX_FETCHED_TEXT_CHARS]
                    continue
                # 后端没带回该页正文 → 进程内直接抓兜底；失败吞进
                # fetch_error，保持整体可用。
                page, error = await fetch_page(
                    client, hit["url"], max_chars=_MAX_FETCHED_TEXT_CHARS
                )
                if error is not None:
                    hit["fetch_error"] = error
                else:
                    hit["fetched_text"] = page["text"]

        return ToolOutcome.success(
            {
                "query": query,
                "engine": provider,
                "results": hits,
                "warnings": warnings,
            }
        )

    async def _search_exa(
        self,
        client: httpx.AsyncClient,
        query: str,
        max_results: int,
    ) -> list[dict]:
        """调 Exa 托管 MCP 的 web_search_exa。无握手：直接 POST 单条
        JSON-RPC tools/call；服务端要求 Accept 同时带 json 与 event-stream，
        响应恒为 SSE（"data: {...}" 行）包装的 JSON-RPC。"""
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "web_search_exa",
                "arguments": {"query": query, "numResults": max_results},
            },
        }
        headers = {"Accept": "application/json, text/event-stream"}
        response = await client.post(
            _EXA_MCP_ENDPOINT, json=payload, headers=headers
        )
        response.raise_for_status()
        message = _parse_sse_jsonrpc(response.text)
        if message is None:
            raise RuntimeError("no JSON-RPC message in Exa MCP response")
        error = message.get("error")
        if isinstance(error, dict):
            raise RuntimeError(f"Exa MCP error: {error.get('message')}")
        result = message.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("Exa MCP response has no result object")
        text = "\n\n---\n\n".join(
            c.get("text", "")
            for c in result.get("content") or []
            if isinstance(c, dict) and c.get("type") == "text"
        )
        if result.get("isError"):
            raise RuntimeError(f"Exa MCP tool error: {text[:300]}")
        return _parse_exa_search_text(text, max_results)

    async def _search_tavily(
        self,
        client: httpx.AsyncClient,
        api_key: str,
        query: str,
        max_results: int,
        *,
        include_raw_content: bool,
    ) -> list[dict]:
        payload = {
            "query": query,
            "max_results": max_results,
            "search_depth": "basic",
            "include_raw_content": include_raw_content,
        }
        headers = {"Authorization": f"Bearer {api_key}"}
        response = await client.post(
            _TAVILY_ENDPOINT, json=payload, headers=headers
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            # 内部不变量被打破（上游返回怪形态）——抛普通异常，由 BaseTool.run
            # 兜底成 internal_tool_error（不属可预期业务失败）。
            raise RuntimeError(
                f"unexpected Tavily response shape: {type(data).__name__}"
            )
        raw = data.get("results")
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
                    "raw_content": item.get("raw_content"),
                    "fetched_text": None,
                    "fetch_error": None,
                }
            )
        return hits


def _parse_sse_jsonrpc(body: str) -> dict | None:
    """从 SSE 文本里拎出 JSON-RPC 消息（带 result 或 error 的最后一条）。

    Exa MCP 的响应形如 "event: message\\ndata: {...}\\n\\n"；按行找 "data:"
    前缀逐条解析，容忍多条消息与解析失败的杂音行。
    """
    message: dict | None = None
    for line in body.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            parsed = json.loads(line[len("data:") :].strip())
        except ValueError:
            continue
        if isinstance(parsed, dict) and ("result" in parsed or "error" in parsed):
            message = parsed
    return message


def _parse_exa_search_text(text: str, max_results: int) -> list[dict]:
    """把 web_search_exa 的单块文本拆成结构化 hits。

    块间以 "---" 独立行分隔；每块形如：
        Title: ...
        URL: ...
        Published: ... / Author: ...   （忽略）
        Highlights:
        <多行摘录，"..." 行是内部省略号分隔（忽略）>
    无 URL 的块丢弃；缺 Highlights 时 snippet 退化为块内剩余非元数据行。
    """
    hits: list[dict] = []
    for block in text.split("\n---\n"):
        block = block.strip()
        if not block:
            continue
        title = ""
        url = ""
        highlight_lines: list[str] = []
        fallback_lines: list[str] = []
        in_highlights = False
        for line in block.splitlines():
            stripped = line.strip()
            if not in_highlights and stripped.startswith("Title: ") and not title:
                title = stripped[len("Title: ") :].strip()
            elif not in_highlights and stripped.startswith("URL: ") and not url:
                url = stripped[len("URL: ") :].strip()
            elif stripped.startswith("Highlights:"):
                in_highlights = True
                rest = stripped[len("Highlights:") :].strip()
                if rest:
                    highlight_lines.append(rest)
            elif in_highlights:
                if stripped and stripped != "...":
                    highlight_lines.append(stripped)
            elif stripped and not stripped.startswith(("Published:", "Author:")):
                fallback_lines.append(stripped)
        if not url:
            continue
        snippet = " ".join(highlight_lines or fallback_lines)
        hits.append(
            {
                "title": title[:200],
                "url": url,
                "snippet": snippet[:1000],
                "raw_content": None,
                "fetched_text": None,
                "fetch_error": None,
            }
        )
        if len(hits) >= max_results:
            break
    return hits

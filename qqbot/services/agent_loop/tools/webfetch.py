"""WebfetchTool — 读取指定 URL 的正文（2026-07-18 新增）。

与 websearch 的分工对齐 Claude Code / OpenCode 的双工具结构：websearch 负责
「找」（关键词 → 链接 + 摘要），webfetch 负责「读」（给定 URL → 正文文本）。
群里有人甩链接、或 websearch 某条结果需要展开时用它。抓取层与 websearch
的正文兜底共用 `_web_common.fetch_page`（普通 HTTP GET + stdlib HTML→纯
文本，无浏览器、不执行 JS），不依赖任何外部服务与 env 配置。

参数：
  url        必填，http/https 绝对地址
  max_chars  可选，正文截断长度，默认 8000（500..20000）

返回：
  {url, final_url, status_code, content_type, title, text, truncated}

错误策略（统一结构化 ToolOutcome，全程无 raise 控制流，见契约 §7.2）：
  - url 缺失 / 非 http(s) / 指向环回内网地址 → invalid_arguments
  - 网络错 / HTTP >= 400 / 非文本类型 / 响应超 5MB → upstream_action_failed
    （对 LLM 是可预期的「对面站点不给看」，不是我们内部坏了）
  - 预料外异常 → BaseTool.run 兜底 internal_tool_error
"""

from __future__ import annotations

from typing import Any

import httpx

from qqbot.core.logging import get_logger
from qqbot.services.agent_loop.prompts import load_sibling_md
from qqbot.services.agent_loop.tool_registry import BaseTool, ToolOutcome
from qqbot.services.agent_loop.tools._web_common import (
    check_public_http_url,
    clamp_int,
    fetch_page,
)

logger = get_logger(__name__)

_DEFAULT_TIMEOUT_SEC = 15.0
_DEFAULT_MAX_CHARS = 8000
_MIN_MAX_CHARS = 500
_MAX_MAX_CHARS = 20000

_USAGE_PROMPT = load_sibling_md(__file__, "webfetch.md")


class WebfetchTool(BaseTool):
    name = "webfetch"
    description = (
        "Fetch a single web page by URL and return its readable body text. "
        "Use it to read a link mentioned in chat, or to expand one websearch "
        "hit whose snippet is insufficient."
    )
    usage_prompt = _USAGE_PROMPT
    # GUEST / 不限 scope：任何人都能让小奏读个链接。
    arguments_schema = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "Absolute http/https URL to fetch.",
            },
            "max_chars": {
                "type": "integer",
                "minimum": _MIN_MAX_CHARS,
                "maximum": _MAX_MAX_CHARS,
                "default": _DEFAULT_MAX_CHARS,
                "description": (
                    "Body text truncation length. Raise only when you truly "
                    "need more of the page."
                ),
            },
        },
        "required": ["url"],
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
        # GUEST + 不限 scope：enforce_access 实为 no-op，但统一保留首行调用。
        if fail := await self.enforce_access(context):
            return fail

        url = (arguments.get("url") or "").strip()
        if not url:
            return ToolOutcome.failure(
                "invalid_arguments", "url is required and must be non-empty"
            )
        reason = check_public_http_url(url)
        if reason is not None:
            return ToolOutcome.failure("invalid_arguments", reason, url=url)
        max_chars = clamp_int(
            arguments.get("max_chars", _DEFAULT_MAX_CHARS),
            _MIN_MAX_CHARS,
            _MAX_MAX_CHARS,
        )

        async with self._client_factory() as client:
            page, error = await fetch_page(client, url, max_chars=max_chars)

        if error is not None:
            return ToolOutcome.failure(
                "upstream_action_failed", f"fetch failed: {error}", url=url
            )
        return ToolOutcome.success({"url": url, **page})

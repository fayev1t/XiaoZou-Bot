"""WebfetchTool — 读取指定 URL 的正文（2026-07-18 新增）。

与 websearch 的分工对齐 Claude Code / OpenCode 的双工具结构：websearch 负责
「找」（关键词 → 链接 + 摘要），webfetch 负责「读」（给定 URL → 正文文本）。
群里有人甩链接、或 websearch 某条结果需要展开时用它。抓取层与 websearch
的正文兜底共用 `_web_common.fetch_page`（普通 HTTP GET + stdlib HTML→纯
文本，无浏览器、不执行 JS），不依赖任何外部服务与 env 配置。

参数：
  url        必填，http/https 绝对地址
  max_chars  可选，抓取正文截断长度，默认 8000（500..20000）；这是送入提炼的
             原文量，不是返回长度
  focus      可选，本次读页的关注点（≤200 字），提炼时优先覆盖

返回：
  {url, final_url, status_code, content_type, title, text, truncated}

  `text` 不是原文（2026-08-03 起）：抓取正文在工具内部经一次 LLM 提炼
  （web_digest，≤1500 字），程序拿到的只是短转述——原文不进入程序 ABI，
  也就不可能被 return 进事件流。提炼不可用时降级为原文截断到同一上限。
  `truncated` 仍描述抓取阶段是否按 max_chars 截断过。

错误策略（统一结构化 ToolOutcome，全程无 raise 控制流，见契约 §7.2）：
  - url 缺失 / 非 http(s) / 指向环回内网地址 / focus 非字符串或超长
    → invalid_arguments
  - 网络错 / HTTP >= 400 / 非文本类型 / 响应超 5MB → upstream_action_failed
    （对 LLM 是可预期的「对面站点不给看」，不是我们内部坏了）
  - 提炼失败**不是**错误：降级为截断原文，查询照常成功
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
from qqbot.services.agent_loop.web_digest import (
    MAX_FOCUS_CHARS,
    digest_or_truncate,
)

logger = get_logger(__name__)

_DEFAULT_TIMEOUT_SEC = 15.0
_DEFAULT_MAX_CHARS = 8000
_MIN_MAX_CHARS = 500
_MAX_MAX_CHARS = 20000

_USAGE_PROMPT = load_sibling_md(__file__, "webfetch.md")


class WebfetchTool(BaseTool):
    name = "webfetch"
    program_kind = "effect"
    max_call_sites = 4
    description = (
        "读取一个公开 HTTP(S) URL，提取网页标题与正文并在内部提炼为不超过"
        " 1500 字的短转述，返回重定向后的最终 URL、状态码、内容类型及抓取"
        "截断状态。"
    )
    usage_prompt = _USAGE_PROMPT
    # GUEST / 不限 scope：任何人都能让小奏读个链接。
    arguments_schema = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "待读取的绝对 HTTP(S) URL；不接受内网或环回地址。",
            },
            "max_chars": {
                "type": "integer",
                "minimum": _MIN_MAX_CHARS,
                "maximum": _MAX_MAX_CHARS,
                "default": _DEFAULT_MAX_CHARS,
                "description": (
                    "抓取正文的截断长度（送入提炼的原文量），默认 8000，"
                    "取值范围为 500–20000；返回的 text 是提炼后的短转述。"
                ),
            },
            "focus": {
                "type": "string",
                "description": (
                    "可选的关注点：这次读页想弄清楚的具体事情，最长 200 个"
                    "字符。提炼会优先完整覆盖关注点相关内容；不填则做全文"
                    "客观提炼。"
                ),
            },
        },
        "required": ["url"],
    }
    result_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "final_url": {"type": "string"},
            "status_code": {"type": "integer"},
            "content_type": {"type": "string"},
            "title": {"type": "string"},
            "text": {"type": "string"},
            "truncated": {"type": "boolean"},
        },
        "required": [
            "url",
            "final_url",
            "status_code",
            "content_type",
            "title",
            "text",
            "truncated",
        ],
        "additionalProperties": False,
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
        # focus 闸门在发请求之前——参数错就不花抓取与提炼两笔开销。
        raw_focus = arguments.get("focus")
        if raw_focus is not None and not isinstance(raw_focus, str):
            return ToolOutcome.failure(
                "invalid_arguments",
                "focus must be a string when provided",
                field="focus",
                reason_code="bad_focus",
            )
        focus = raw_focus.strip() if isinstance(raw_focus, str) else None
        if focus is not None and len(focus) > MAX_FOCUS_CHARS:
            return ToolOutcome.failure(
                "invalid_arguments",
                f"focus must be at most {MAX_FOCUS_CHARS} chars, got "
                f"{len(focus)} — state one concern, do not paste the "
                "conversation.",
                field="focus",
                reason_code="focus_too_long",
            )

        async with self._client_factory() as client:
            page, error = await fetch_page(client, url, max_chars=max_chars)

        if error is not None:
            return ToolOutcome.failure(
                "upstream_action_failed", f"fetch failed: {error}", url=url
            )
        assert page is not None
        # 抓取正文只在这里活到提炼为止：程序 ABI 里的 text 恒为有界短文
        # （提炼 ≤1500 字；提炼不可用时截断原文到同一上限）。
        page["text"] = await digest_or_truncate(
            page["text"],
            url=url,
            title=str(page.get("title") or ""),
            focus=focus,
        )
        return ToolOutcome.success({"url": url, **page})

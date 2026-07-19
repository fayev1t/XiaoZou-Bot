"""网页抓取公共层 —— websearch（正文兜底）与 webfetch（读指定 URL）共用。

与 `_onebot_common.py` 同位：把跨工具的公共出站约定收敛在一处。思路对齐
Claude Code / OpenCode 的 webfetch 实现：普通 HTTP GET 拿页面、进程内把
HTML 转成纯文本，**无浏览器、不执行 JS**——JS 渲染页面拿不到正文是已
接受的取舍。不引第三方解析依赖（html2text / bs4 / trafilatura），stdlib
HTMLParser 的提取质量对 LLM 消费已够用，服务器端零新增安装。

导出：
  clamp_int(value, lo, hi)      参数钳制（原 websearch._clamp_int 上移共享）
  check_public_http_url(url)    URL 闸门：仅 http/https + 非环回/内网地址
  html_to_text(html)            HTML → (title, 正文纯文本)
  fetch_page(client, url, ...)  GET → 类型/大小闸门 → 提取正文

错误约定（工具侧全程无 raise 控制流，见 tool_registry 模块 docstring）：
  - check_public_http_url **返回**原因串（None = 通过），由调用方折
    invalid_arguments。
  - fetch_page **返回** (page, error) 二元组：可预期失败（网络错 / HTTP
    状态 / 非文本类型 / 超大响应）都折进 error 字符串，httpx.HTTPError 在
    helper 内消化不外抛；预料外异常照常上抛由 BaseTool.run 兜底。
"""

from __future__ import annotations

import ipaddress
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit

import httpx

# 整页响应字节上限（与 opencode webfetch 的 5MB 同档），防一次拉爆内存。
_MAX_CONTENT_BYTES = 5 * 1024 * 1024

# 子树整体丢弃的标签（脚本/样式/不可见内容）。
_SKIP_SUBTREES = frozenset(
    {"script", "style", "noscript", "template", "svg", "iframe"}
)
# 前后补换行的块级标签，保住段落结构的可读性。
_BLOCK_TAGS = frozenset(
    {
        "p",
        "div",
        "section",
        "article",
        "header",
        "footer",
        "nav",
        "aside",
        "main",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "ul",
        "ol",
        "li",
        "dl",
        "dt",
        "dd",
        "table",
        "thead",
        "tbody",
        "tr",
        "blockquote",
        "pre",
        "figure",
        "figcaption",
        "form",
    }
)


def clamp_int(value: Any, lo: int, hi: int) -> int:
    """把 LLM 给的参数钳到 [lo, hi]；非法输入保守取 lo。"""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return lo
    return max(lo, min(hi, v))


def check_public_http_url(url: str) -> str | None:
    """URL 闸门：仅放行 http/https 且 host 非环回/内网的地址。

    不合法**返回**人话原因串（调用方折 invalid_arguments），通过返回 None。
    只挡直接指向环回/私网/链路本地的 IP 字面量与 localhost 名称（SSRF 基础
    防护）；域名解析后指向内网、或重定向落到内网的绕过不在此层处理——
    接受的残余风险（bot 服务器上没有可被这条路径窃取的内部凭据面）。
    """
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https"):
        return f"unsupported URL scheme: {parsed.scheme or '(empty)'}"
    host = (parsed.hostname or "").lower()
    if not host:
        return "URL has no host"
    if host == "localhost" or host.endswith(".localhost"):
        return "refusing to fetch a localhost address"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None  # 域名，放行
    if not ip.is_global:
        return f"refusing to fetch non-public address: {host}"
    return None


class _TextExtractor(HTMLParser):
    """一次遍历 HTML：收 <title>、按块级标签断行、整树跳过脚本/样式。

    HTMLParser 对残缺 HTML 自身容错（不 raise），convert_charrefs=True
    让实体（&amp; 等）在 handle_data 前已解码。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._title_chunks: list[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag in _SKIP_SUBTREES:
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
            return
        if self._skip_depth == 0 and (tag in _BLOCK_TAGS or tag in ("br", "hr")):
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_SUBTREES:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "title":
            self._in_title = False
            return
        if self._skip_depth == 0 and tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_chunks.append(data)
            return
        if self._skip_depth == 0 and data:
            self._chunks.append(data)

    @property
    def title(self) -> str:
        return " ".join("".join(self._title_chunks).split())

    @property
    def text(self) -> str:
        """逐行压空白 + 连续空行折叠成一个，产出紧凑可读的纯文本。"""
        lines = [" ".join(ln.split()) for ln in "".join(self._chunks).splitlines()]
        out: list[str] = []
        prev_blank = True  # 开头空行直接丢
        for line in lines:
            if line:
                out.append(line)
                prev_blank = False
            elif not prev_blank:
                out.append("")
                prev_blank = True
        while out and not out[-1]:
            out.pop()
        return "\n".join(out)


def html_to_text(html: str) -> tuple[str, str]:
    """HTML → (title, 正文纯文本)。残缺 HTML 由 HTMLParser 容错消化。"""
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    return parser.title, parser.text


def _bare_content_type(content_type: str) -> str:
    """去掉 ;charset=... 参数、压成小写的裸 MIME 类型。"""
    return (content_type or "").split(";", 1)[0].strip().lower()


def _is_textual_content_type(content_type: str) -> bool:
    """text/* 与 json/xml 族放行；无 Content-Type 乐观按文本（有大小上限兜着）。"""
    ct = _bare_content_type(content_type)
    if not ct:
        return True
    return ct.startswith("text/") or "json" in ct or "xml" in ct


def _looks_like_html(content_type: str, body: str) -> bool:
    """决定走 HTML 结构提取还是原文透传；无类型头时嗅探文档开头。"""
    ct = _bare_content_type(content_type)
    if "html" in ct or "xml" in ct:
        return True
    if not ct:
        return body.lstrip()[:100].lower().startswith(("<!doctype", "<html"))
    return False


async def fetch_page(
    client: Any, url: str, *, max_chars: int
) -> tuple[dict | None, str | None]:
    """GET 一个 URL 并提取正文。**返回** (page, error)，error 非 None 即
    可预期失败（原因串给 LLM 看得懂的人话）。page 结构：

        {final_url, status_code, content_type, title, text, truncated}

    text 已按 max_chars 截断（truncated 标记是否截过）；HTML/XML 走
    html_to_text 提取，text/plain、JSON 等原文透传（title 为空串）。
    """
    try:
        response = await client.get(url)
    except httpx.HTTPError as exc:
        return None, f"request failed: {type(exc).__name__}: {exc}"
    status = int(getattr(response, "status_code", 0) or 0)
    if status >= 400:
        return None, f"HTTP {status}"
    content = getattr(response, "content", b"") or b""
    if len(content) > _MAX_CONTENT_BYTES:
        return None, (
            f"response too large: {len(content)} bytes > {_MAX_CONTENT_BYTES}"
        )
    headers = getattr(response, "headers", None)
    content_type = str(headers.get("content-type") or "") if headers else ""
    if not _is_textual_content_type(content_type):
        return None, f"unsupported content type: {_bare_content_type(content_type)}"
    body = response.text
    if _looks_like_html(content_type, body):
        title, text = html_to_text(body)
    else:
        title, text = "", body.strip()
    return {
        "final_url": str(getattr(response, "url", "") or url),
        "status_code": status,
        "content_type": _bare_content_type(content_type),
        "title": title,
        "text": text[:max_chars],
        "truncated": len(text) > max_chars,
    }, None

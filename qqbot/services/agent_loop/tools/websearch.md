# 工具：`websearch`

## 功能

`websearch` 按关键词检索公开网页，返回结构化搜索结果，并可为前若干条结果
附加正文。后端由 `WEBSEARCH_PROVIDER` 选择：默认 `exa`，可选
`tavily`。

## 参数

- `query`：必填非空字符串，自然语言搜索关键词。
- `fetch_top_n`：可选整数，默认 0，范围 0–5。大于 0 时，为前 N 条结果附加
  `fetched_text`（以 `query` 为关注点提炼后的正文转述）；正文抓取失败时
  对应结果包含 `fetch_error`。
- `max_results`：可选整数，默认 10，范围 1–20，表示返回结果数量上限。

## 权限与作用域

`required_permission=GUEST`，`allowed_scopes` 不限，不要求机器人群角色。

## 返回

成功返回：

```json
{
  "query": "搜索词",
  "engine": "exa",
  "results": [
    {
      "title": "标题",
      "url": "https://example.com/",
      "snippet": "摘要",
      "fetched_text": null,
      "fetch_error": null
    }
  ],
  "warnings": []
}
```

`fetched_text` 仅在 `fetch_top_n` 覆盖该结果且正文读取成功时填充。它不是
原文：抓取正文（单条最多 8000 字）会以 `query` 为关注点提炼成不超过
1500 字的转述，页面与 `query` 无关时会如实说明；提炼暂不可用时退化为原文
截断到同一长度。单条正文读取失败不会使整次搜索失败，而是写入该条
`fetch_error`。需要对某条结果换一个关注点细读时，用 `webfetch` 带 `focus`
再读该 URL。

`query` 为空时返回 `invalid_arguments`。后端配置无效时返回
`internal_tool_error`；搜索服务或网络异常由工具基类归一为
`internal_tool_error`。

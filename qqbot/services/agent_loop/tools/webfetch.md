# 工具：`webfetch`

## 功能

`webfetch` 读取一个指定的公开网页，提取标题与可读正文，并把正文提炼成一段
不超过 1500 字的短转述后返回。抓取使用普通 HTTP 请求，不执行 JavaScript，
也不携带登录状态。

## 参数

- `url`：必填非空字符串，必须是绝对 HTTP(S) URL。环回地址和私有网络地址会
  在请求前被拒绝。
- `max_chars`：可选整数，默认 8000，范围 500–20000，表示抓取正文的截断长度
  （送入提炼的原文量，不是返回长度）。
- `focus`：可选字符串，最长 200 个字符——这次读页想弄清楚的具体事情。提炼
  会优先完整覆盖关注点相关内容，页面没提到时 `text` 会写明「页面未提及」；
  不填则做全文客观提炼。

## 权限与作用域

`required_permission=GUEST`，`allowed_scopes` 不限，不要求机器人群角色。

## 返回

成功返回：

```json
{
  "url": "https://example.com/start",
  "final_url": "https://example.com/final",
  "status_code": 200,
  "content_type": "text/html",
  "title": "页面标题",
  "text": "提炼后的正文转述",
  "truncated": false
}
```

`text` 是正文的提炼转述（保留关键事实、数字与结论，不超过 1500 字），不是
原文全文；提炼暂不可用时退化为原文截断到同一长度。`final_url` 是重定向后的
地址；`truncated=true` 表示抓取阶段正文已按 `max_chars` 截断（此时提炼只
覆盖被抓到的部分）。脚本、样式等非正文内容不会包含在 `text` 中。

URL 缺失、协议不支持、目标为非公开地址或 `focus` 超长时返回
`invalid_arguments`。网络错误、HTTP 错误、非文本响应或响应体超限时返回
`upstream_action_failed`。

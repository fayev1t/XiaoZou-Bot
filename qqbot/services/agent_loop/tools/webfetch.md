# 工具：`webfetch`

## 功能

`webfetch` 读取一个指定的公开网页，提取标题与可读正文。抓取使用普通 HTTP
请求，不执行 JavaScript，也不携带登录状态。

## 参数

- `url`：必填非空字符串，必须是绝对 HTTP(S) URL。环回地址和私有网络地址会
  在请求前被拒绝。
- `max_chars`：可选整数，默认 8000，范围 500–20000，表示返回正文的截断长度。

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
  "text": "提取后的正文",
  "truncated": false
}
```

`final_url` 是重定向后的地址；`truncated=true` 表示正文已按
`max_chars` 截断。脚本、样式等非正文内容不会包含在 `text` 中。

URL 缺失、协议不支持或目标为非公开地址时返回 `invalid_arguments`。网络错误、
HTTP 错误、非文本响应或响应体超限时返回 `upstream_action_failed`。

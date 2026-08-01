# 工具：`group_notice`

## 功能

`group_notice` 在当前群发布群公告，对应 napcat 扩展 action
`_send_group_notice`。该 action 通过 `bot.call_api` 调用。

## 参数

```json
{
  "content": "公告正文",
  "image": "https://example.com/banner.png"
}
```

- `content`：必填非空字符串，表示公告正文；仅包含空白字符时参数无效。
- `image`：可选字符串，表示附图 URL 或文件路径。
- `group_id` 从当前 `scope_key` 注入，参数中不存在 `group_id`。

调用成功后公告对当前群成员可见。

## 权限与作用域

- `allowed_scopes=("group",)`。
- `required_permission=OWNER`。`triggered_by_event_id` 所指消息的发送者会在
  调用时按实时群角色校验。
- `required_bot_role="admin"`，群主同时满足该条件。

## 返回

成功返回 `{"ok":true,"group_id":<int>}`。参数无效时返回
`invalid_arguments`；权限条件不满足时返回 `permission_denied_user_tier`
或 `permission_denied_bot_role`；OneBot 调用失败时返回
`upstream_action_failed`。

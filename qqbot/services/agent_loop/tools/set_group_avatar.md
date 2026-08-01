# 工具：`set_group_avatar`

## 功能

`set_group_avatar` 设置当前群的群头像，对应 OneBot V11
`set_group_portrait`。调用成功后，新头像对当前群成员可见。

## 参数

```json
{
  "file": "https://example.com/avatar.png"
}
```

- `file`：必填非空字符串，支持 HTTP(S) URL、本地文件路径或 base64 字符串。
- `group_id` 从当前 `scope_key` 注入，参数中不存在 `group_id`。
- 工具不生成图片来源；`file` 的解析与下载由 napcat 处理。

## 权限与作用域

- `allowed_scopes=("group",)`。
- `required_permission=OWNER`。`triggered_by_event_id` 所指消息的发送者会在
  调用时按实时群角色校验。
- `required_bot_role="admin"`，群主同时满足该条件。

## 返回

成功返回 `{"ok":true,"group_id":<int>}`。参数无效时返回
`invalid_arguments`；权限条件不满足时返回 `permission_denied_user_tier`
或 `permission_denied_bot_role`；图片来源或 OneBot 调用失败时返回
`upstream_action_failed`。

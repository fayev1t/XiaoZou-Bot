# 工具：`set_group_name`

## 功能

`set_group_name` 修改当前群的群名称，对应 OneBot V11
`set_group_name`。调用成功后，新群名对当前群成员可见。

## 参数

```json
{
  "name": "新的群名称"
}
```

- `name`：必填非空字符串，表示新的群名称；仅包含空白字符时参数无效。
- `group_id` 从当前 `scope_key` 注入，参数中不存在 `group_id`。
- 工具会将 `name` 映射为 OneBot 的 `group_name` 参数。

## 权限与作用域

- `allowed_scopes=("group",)`。
- `required_permission=OWNER`。`triggered_by_event_id` 所指消息的发送者会在
  调用时按实时群角色校验。
- `required_bot_role="admin"`，群主同时满足该条件。

## 返回

成功返回 `ok`、`group_id` 和 `group_name`。参数无效时返回
`invalid_arguments`；权限条件不满足时返回 `permission_denied_user_tier`
或 `permission_denied_bot_role`；OneBot 调用失败时返回
`upstream_action_failed`。

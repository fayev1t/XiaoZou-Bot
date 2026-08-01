# 工具：`set_essence`

## 功能

`set_essence` 将指定消息加入群精华列表或从列表移除。`action=set` 对应
OneBot V11 `set_essence_msg`，`action=delete` 对应
`delete_essence_msg`。

## 参数

```json
{
  "message_id": 123456,
  "action": "set"
}
```

- `message_id`：必填整数，目标消息的 `onebot_message_id`，对应时间线消息的
  `id` 属性。
- `action`：可选字符串，默认 `set`；支持 `set` 和 `delete`。
- OneBot 按 `message_id` 定位消息，参数中不存在 `group_id`；工具仍要求当前
  scope 为 group。

## 权限与作用域

- `allowed_scopes=("group",)`。
- `required_permission=OWNER`。`triggered_by_event_id` 所指消息的发送者会在
  调用时按实时群角色校验。
- `required_bot_role="admin"`，群主同时满足该条件。

## 返回

成功返回 `ok`、`message_id` 和 `action`。参数无效时返回
`invalid_arguments`；权限条件不满足时返回 `permission_denied_user_tier`
或 `permission_denied_bot_role`；OneBot 调用失败时返回
`upstream_action_failed`。

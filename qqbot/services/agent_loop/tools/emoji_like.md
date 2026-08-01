# 工具：`emoji_like`

## 功能

`emoji_like` 为指定消息添加或移除 QQ 表情回应，对应 OneBot V11
`set_msg_emoji_like`。

## 参数

```json
{
  "message_id": 123456,
  "emoji_id": "128",
  "set": true
}
```

- `message_id`：必填整数，目标消息的 `onebot_message_id`，对应时间线消息的
  `id` 属性。
- `emoji_id`：必填字符串，QQ 表情或 face 的 ID。执行层也接受可转换为字符串的
  数值。
- `set`：可选布尔值，默认 `true`；`true` 表示添加，`false` 表示移除。
- OneBot 按 `message_id` 定位消息，参数中不存在 `group_id`；工具仍要求当前
  scope 为 group。

## 权限与作用域

`allowed_scopes=("group",)`，`required_permission=GUEST`，不要求机器人
群角色。

## 返回

成功返回 `ok`、`message_id`、`emoji_id` 和 `set`。参数无效时返回
`invalid_arguments`；OneBot 调用失败时返回 `upstream_action_failed`。

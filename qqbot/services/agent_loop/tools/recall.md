# 工具：`recall`

## 功能

`recall` 撤回当前群中的一条消息，对应 OneBot V11 `delete_msg`。OneBot 按
`message_id` 定位消息，参数中不存在 `group_id`；工具仍要求当前 scope 为
group。

## 参数

```json
{
  "message_id": 123456
}
```

`message_id` 为必填整数，表示目标消息的 `onebot_message_id`，对应时间线
消息的 `id` 属性。

## 权限与作用域

- `allowed_scopes=("group",)`，`required_permission=GUEST`。
- 撤回机器人自身消息不要求机器人群角色。
- 撤回其他成员消息时，机器人须为 `admin` 或 `owner`，且角色严格高于消息
  作者。
- 工具会通过 `get_msg` 查询消息作者；作者或角色无法解析时，由 OneBot 执行
  最终权限判定。

## 返回

成功返回 `{"ok":true,"message_id":<int>}`。参数无效时返回
`invalid_arguments`；前置权限条件不满足时返回
`permission_denied_bot_role`；OneBot 调用失败时返回
`upstream_action_failed`。

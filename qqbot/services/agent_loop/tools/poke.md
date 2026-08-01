# 工具：`poke`

## 功能

`poke` 向当前群中的指定成员发送一次戳一戳，对应 OneBot V11
`group_poke`。调用成功后，平台产生对应的戳一戳事件。

## 参数

```json
{
  "user_id": 12345
}
```

`user_id` 为必填整数，表示目标成员的 QQ 号。`group_id` 从当前
`scope_key` 注入，参数中不存在 `group_id`。

## 权限与作用域

`allowed_scopes=("group",)`，`required_permission=GUEST`，不要求机器人
群角色。

## 返回

成功返回 `ok`、`group_id` 和 `user_id`。参数无效时返回
`invalid_arguments`；OneBot 调用失败时返回 `upstream_action_failed`。

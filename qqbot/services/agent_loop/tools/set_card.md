# 工具：`set_card`

## 功能

`set_card` 设置或清空当前群中指定成员的群名片，对应 OneBot V11
`set_group_card`。

## 参数

```json
{
  "user_id": 12345,
  "card": "新名片"
}
```

- `user_id`：必填整数，表示目标成员的 QQ 号。
- `card`：可选字符串，默认空字符串；空字符串表示清空群名片。
- `group_id` 从当前 `scope_key` 注入，参数中不存在 `group_id`。

## 权限与作用域

- `allowed_scopes=("group",)`。
- `required_permission=ADMIN`。`triggered_by_event_id` 所指消息的发送者会在
  调用时按实时群角色校验。
- `required_bot_role="admin"`。
- 修改机器人自身群名片不执行目标层级检查；修改其他成员时，机器人群角色必须
  严格高于目标成员。

## 返回

成功返回 `ok`、`group_id`、`user_id` 和 `card`。权限条件不满足时返回
`permission_denied_user_tier` 或 `permission_denied_bot_role`；OneBot
调用失败时返回 `upstream_action_failed`。

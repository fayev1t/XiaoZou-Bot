# 工具：`set_admin`

## 功能

`set_admin` 授予或撤销当前群中指定成员的管理员身份，对应 OneBot V11
`set_group_admin`。

## 参数

```json
{
  "user_id": 12345,
  "enable": true
}
```

- `user_id`：必填整数，表示目标成员的 QQ 号。
- `enable`：可选布尔值，默认 `true`；`true` 表示授予管理员，
  `false` 表示撤销管理员。
- `group_id` 从当前 `scope_key` 注入，参数中不存在 `group_id`。

## 权限与作用域

- `allowed_scopes=("group",)`。
- `required_permission=OWNER`。`triggered_by_event_id` 所指消息的发送者会在
  调用时按实时群角色校验。
- `required_bot_role="owner"`；机器人为普通管理员时不满足条件。

## 返回

成功返回 `ok`、`group_id`、`user_id` 和 `enable`。权限条件不满足时返回
`permission_denied_user_tier` 或 `permission_denied_bot_role`；OneBot
调用失败时返回 `upstream_action_failed`。

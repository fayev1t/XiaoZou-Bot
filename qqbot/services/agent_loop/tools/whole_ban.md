# 工具：`whole_ban`

## 功能

`whole_ban` 开启或关闭当前群的全员禁言，对应 OneBot V11
`set_group_whole_ban`。开启后，普通成员不能发言，群管理员和群主仍可发言。

## 参数

```json
{
  "enable": true
}
```

- `enable`：可选布尔值，默认 `true`；`true` 表示开启全员禁言，
  `false` 表示关闭。
- `group_id` 从当前 `scope_key` 注入，参数中不存在 `group_id`。

## 权限与作用域

- `allowed_scopes=("group",)`。
- `required_permission=OWNER`。`triggered_by_event_id` 所指消息的发送者会在
  调用时按实时群角色校验。
- `required_bot_role="admin"`，群主同时满足该条件。

## 返回

成功返回 `ok`、`group_id` 和 `enable`。权限条件不满足时返回
`permission_denied_user_tier` 或 `permission_denied_bot_role`；OneBot
调用失败时返回 `upstream_action_failed`。

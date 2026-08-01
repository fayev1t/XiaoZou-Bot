# 工具：`ban`

## 功能

`ban` 设置或解除当前群中指定成员的禁言，对应 OneBot V11
`set_group_ban`。

## 参数

```json
{
  "user_id": 12345,
  "duration": 1800
}
```

- `user_id`：必填整数，表示目标成员的 QQ 号。
- `duration`：可选整数，默认 1800，单位为秒；`0` 表示解除禁言。
- `group_id` 从当前 `scope_key` 注入，参数中不存在 `group_id`。

## 权限与作用域

- `allowed_scopes=("group",)`。
- `required_permission=ADMIN`。`triggered_by_event_id` 所指消息的发送者会在
  调用时按实时群角色校验。
- `required_bot_role="admin"`。
- 机器人群角色必须严格高于目标成员；目标角色可查询时会在调用 OneBot 前校验。

## 返回

成功返回 `ok`、`group_id`、`user_id` 和 `duration`。权限条件不满足时
返回 `permission_denied_user_tier` 或 `permission_denied_bot_role`；
OneBot 调用失败时返回 `upstream_action_failed`。

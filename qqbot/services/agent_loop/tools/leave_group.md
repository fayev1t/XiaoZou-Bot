# 工具：`leave_group`

## 功能

`leave_group` 使机器人退出当前群，对应 OneBot V11 `set_group_leave`。
`is_dismiss=true` 时改为解散整个群。

普通退出成功后，机器人不再接收或发送该群消息；解散成功后，群对所有成员关闭。
本工具不提供撤销操作。

## 参数

```json
{
  "is_dismiss": false
}
```

- `is_dismiss`：可选布尔值，默认 `false`。`false` 表示仅退出当前群；
  `true` 表示解散整个群。
- `group_id` 从当前 `scope_key` 注入，参数中不存在 `group_id`。
- `is_dismiss=true` 且机器人不是群主时，调用在执行 OneBot 前失败，不会退化为
  普通退出。

## 权限与作用域

- `allowed_scopes=("group",)`。
- `required_permission=OWNER`。`triggered_by_event_id` 所指消息的发送者会在
  调用时按实时群角色校验。
- 普通退出不要求机器人群角色。
- 解散要求机器人群角色为 `owner`。

## 返回

成功返回 `ok`、`group_id` 和 `is_dismiss`。权限条件不满足时返回
`permission_denied_user_tier` 或 `permission_denied_bot_role`；OneBot
调用失败时返回 `upstream_action_failed`。

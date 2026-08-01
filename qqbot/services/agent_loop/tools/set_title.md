# 工具：`set_title`

## 功能

`set_title` 设置或清空当前群中指定成员的专属头衔，对应 OneBot V11
`set_group_special_title`。

## 参数

```json
{
  "user_id": 12345,
  "title": "专属头衔"
}
```

- `user_id`：必填整数，表示目标成员的 QQ 号。
- `title`：可选字符串，默认空字符串；空字符串表示清空专属头衔。
- `group_id` 从当前 `scope_key` 注入，参数中不存在 `group_id`。
- 工具会将 `title` 映射为 OneBot 的 `special_title` 参数。

## 权限与作用域

- `allowed_scopes=("group",)`。
- `required_permission=OWNER`。`triggered_by_event_id` 所指消息的发送者会在
  调用时按实时群角色校验。
- `required_bot_role="owner"`；机器人为普通管理员时不满足条件。

## 返回

成功返回 `ok`、`group_id`、`user_id` 和 `title`。权限条件不满足时返回
`permission_denied_user_tier` 或 `permission_denied_bot_role`；OneBot
调用失败时返回 `upstream_action_failed`。

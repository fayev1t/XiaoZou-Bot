# 工具：`get_member_info`

## 功能

`get_member_info` 查询当前群中一个成员的实时资料，对应 OneBot V11
`get_group_member_info`，并使用 `no_cache=true`。该调用为只读操作。

## 参数

```json
{
  "user_id": 12345
}
```

- `user_id`：必填整数，表示目标成员的 QQ 号。
- `group_id` 从当前 `scope_key` 注入，参数中不存在 `group_id`。

## 权限与作用域

`allowed_scopes=("group",)`，`required_permission=GUEST`，不要求机器人
群角色。

## 返回

成功返回以下字段：

```json
{
  "user_id": 12345,
  "nickname": "昵称",
  "card": "群名片",
  "role": "member",
  "level": "1",
  "title": "",
  "join_time": "2026-01-01T12:00:00+08:00",
  "last_sent_time": "2026-07-31T12:00:00+08:00",
  "banned_until": null
}
```

`join_time`、`last_sent_time` 和非空的 `banned_until` 使用
Asia/Shanghai 时区的 ISO8601 格式。平台没有对应时间或成员未处于禁言状态时
值为 `null`。OneBot 调用失败时返回 `upstream_action_failed`。

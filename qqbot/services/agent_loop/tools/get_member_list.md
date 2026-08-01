# 工具：`get_member_list`

## 功能

`get_member_list` 查询当前群的成员列表，对应 OneBot V11
`get_group_member_list`。该调用为只读操作，返回列表会按参数截断。

## 参数

```json
{
  "limit": 200,
  "role": "admin",
  "include_activity": false
}
```

- `limit`：可选整数，默认 200，最小按 1 处理，最大按 500 处理。仅限制
  `members` 数组长度，不影响完整总数 `count`。
- `role`：可选字符串，支持 `owner`、`admin`、`member`。角色过滤先于
  `limit` 截断。
- `include_activity`：可选布尔值，默认 `false`。为 `true` 时，每个条目
  增加 `join_time` 和 `last_sent_time`。

`group_id` 从当前 `scope_key` 注入，参数中不存在 `group_id`。

## 权限与作用域

`allowed_scopes=("group",)`，`required_permission=GUEST`，不要求机器人
群角色。

## 返回

成功返回：

```json
{
  "count": 350,
  "matched": 4,
  "members": [
    {"user_id": 123, "nickname": "昵称", "card": "群名片", "role": "admin"}
  ]
}
```

- `count` 是未过滤的完整群成员数。
- `matched` 是通过 `role` 过滤的成员数；未提供 `role` 时等于 `count`。
- `members` 是过滤后按 `limit` 截断的数组，基础字段为 `user_id`、
  `nickname`、`card`、`role`。
- 当前处于禁言状态的成员额外包含 Asia/Shanghai ISO8601 格式的
  `banned_until`。
- `include_activity=true` 时额外包含 `join_time` 和 `last_sent_time`；
  平台缺少时间时对应值可以为 `null`。

`matched > len(members)` 表示返回结果已截断。OneBot 调用失败时返回
`upstream_action_failed`。

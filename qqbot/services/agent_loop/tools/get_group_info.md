# 工具：`get_group_info`

## 功能

`get_group_info` 查询当前群的基础资料，对应 OneBot V11
`get_group_info`，并使用 `no_cache=true` 获取实时数据。该调用为只读操作。

## 参数

```json
{}
```

本工具不接收业务参数。`group_id` 从当前 `scope_key` 注入，参数中不存在
`group_id`。

## 权限与作用域

`allowed_scopes=("group",)`，`required_permission=GUEST`，不要求机器人
群角色。

## 返回

成功结果固定包含：

- `group_id`
- `group_name`
- `member_count`
- `max_member_count`

平台提供对应数据时，结果还包含 `group_remark` 和
`group_create_time`。`group_create_time` 使用 Asia/Shanghai 时区的
ISO8601 格式。未提供的可选字段不会出现在结果中。

OneBot 调用失败时返回 `upstream_action_failed`。

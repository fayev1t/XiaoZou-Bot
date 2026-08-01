# 工具：`get_group_honor`

## 功能

`get_group_honor` 查询当前群的荣誉与榜单信息，对应 OneBot V11
`get_group_honor_info`。该调用为只读操作。

## 参数

```json
{
  "type": "all"
}
```

`type` 为可选字符串，默认 `all`，支持 `talkative`、`performer`、
`legend`、`strong_newbie`、`emotion` 和 `all`。`group_id` 从当前
`scope_key` 注入，参数中不存在 `group_id`。

## 权限与作用域

`allowed_scopes=("group",)`，`required_permission=GUEST`，不要求机器人
群角色。

## 返回

成功结果包含 `group_id`、`type`，以及平台提供时的
`current_talkative` 和一个或多个 `*_list`。每个榜单最多返回前 5 条，每条
仅保留 `user_id`、`nickname`、`description`。没有数据的榜单不会出现在
结果中。

OneBot 调用失败时返回 `upstream_action_failed`。

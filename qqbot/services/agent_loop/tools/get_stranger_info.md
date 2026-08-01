# 工具：`get_stranger_info`

## 功能

`get_stranger_info` 按 QQ 号查询用户的公开基础资料，对应 OneBot V11
`get_stranger_info`。查询不依赖目标用户是否属于当前群，且为只读操作。

## 参数

```json
{
  "user_id": 12345
}
```

`user_id` 为必填整数，表示待查询用户的 QQ 号。本工具不接收或使用
`group_id`。

## 权限与作用域

`required_permission=GUEST`，`allowed_scopes` 不限，不要求机器人群角色。

## 返回

成功返回 `user_id`、`nickname`、`sex` 和 `age`。平台未公开对应信息时，
`sex` 可以为 `unknown`，`age` 可以为 `0`。OneBot 调用失败时返回
`upstream_action_failed`。

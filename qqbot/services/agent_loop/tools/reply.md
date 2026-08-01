# 工具：`reply`

## 功能

`reply` 为当前 scope 起一段短时等待，表示**你要开口了，但字还没发出去**。
该工具不发送消息，也不保存任何内容。

这段等待同时覆盖两件事：把这些字打出来本来就需要时间；而这段时间里对方可能
还在往下说。等待结束时系统写入 `<reply-task-completed>` 并唤醒当前 scope，
那一拍你面对的是**到那一刻为止的完整时间线**——说什么、还说不说，都在那时
决定。可见消息由 `send_messages` 单独发送。

## 普通分支

普通分支只接收一个参数：

```json
{"hold_seconds": 8}
```

- `hold_seconds`：必填整数，范围为 0–90，无默认值。

等多久是每次都要现场判断的事：估一下这句话你要打多久，再想想对方是已经说完
了、还是话才说到一半。没有内容参数——想说的话不写在这里，也不需要在这里先
把局势想透，那正是等待结束后那一拍要做的事。

## 修订语义

当前 scope 同时最多存在一段等待。每次普通调用都会追加一条修订：

- 最新修订的 `hold_seconds` 完整替换旧值，因此可以延长，也可以缩短。
- 旧修订仍作为 `<tool-call name="reply">` 历史记录保留在时间线上，你能看见
  自己已经续过几次。
- 硬截止时间固定为首次创建后的 90 秒；后续修订不延长该硬截止时间。
- 等待结束只生成 `<reply-task-completed>` 和新 tick，不触发消息发送。

普通分支不接收 `reply_task_id`，也不存在 `upsert` action。

## 取消分支

```json
{"action": "cancel"}
```

`action="cancel"` 撤销当前 scope 的等待。`reply_task_id` 为可选断言：提供时
必须与当前等待的 ID 一致；省略时撤销当前 scope 中存在的那一段等待。取消分支
不接收 `hold_seconds`。已经结束的等待不能再取消。

## 返回

普通分支成功返回 `reply_task_id`、`revision`、`state`、`flush_at` 和
`hard_deadline`。成功仅表示等待已开始，不表示任何消息已发送，也不包含
`message_id`。

取消分支成功返回被撤销等待的标识与状态。参数形状错误会返回
`invalid_arguments`；旧字段 `analysis`、`brief`、`targets`、`gist`、
`points`、`mode`、`messages`、`verbatim_messages` 和 `expected_revision`
会返回对应的迁移 `reason_code`。

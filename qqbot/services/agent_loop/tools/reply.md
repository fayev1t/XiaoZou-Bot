# 工具：`reply`

## 功能

`reply` 为当前 scope 起一段短时等待，表示**准备开口，但消息尚未发出**。
该工具不发送消息，也不保存任何内容。

这段等待同时覆盖两件事：输入消息本身需要时间；等待期间对方可能继续发言。
等待结束时系统写入 `<等待结束>` 行并唤醒当前 scope。新 tick 会读取
**到那一刻为止的完整时间线**，是否发言及具体内容均在那时决定。可见消息由
`send_messages` 单独发送。

## 普通分支

普通分支只接收一个参数：

```json
{"hold_seconds": 8}
```

- `hold_seconds`：必填整数，范围为 0–90，无默认值。

等待时长需根据预计输入耗时以及对方是否仍在发言现场判断。没有内容参数——消息
正文不写在这里，也无须预先形成局势分析；这些内容均在等待结束后的新 tick 决定。

## 修订语义

当前 scope 同时最多存在一段等待。每次普通调用都会追加一条修订：

- 最新修订的 `hold_seconds` 完整替换旧值，因此可以延长，也可以缩短。
- 旧修订仍作为 `<工具>reply` 历史行保留在时间线上，可据此识别
  已经发生的续期。
- 硬截止时间固定为首次创建后的 90 秒；后续修订不延长该硬截止时间。
- 等待结束只生成 `<等待结束>` 行和新 tick，不触发消息发送。

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

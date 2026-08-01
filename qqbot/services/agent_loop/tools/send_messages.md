# 工具：`send_messages`

## 功能

`send_messages` 向当前群发送 1–10 个有序气泡。`messages` 是唯一业务参数，
目标群由当前 `scope_key` 注入，不存在 `target` 或 `group_id` 参数。

标准发言链路由 `reply` 起一段等待、`<reply-task-completed>` 唤醒以及
`send_messages` 发送组成。落笔依据是唤醒那一刻的时间线，不是决定开口时的
判断。

## 参数

```json
{
  "messages": [
    {
      "kind": "chat",
      "content": [
        {"type": "text", "data": {"text": "周日会提前关门，最好五点前到"}}
      ]
    },
    {"kind": "meme", "image_hash": "<saved-memes 中的 64 位 sha256>"}
  ]
}
```

`messages` 为非空数组，最多 10 项，按数组顺序发送。每项支持以下一种结构。

### chat 气泡

```json
{"kind": "chat", "content": [{"type": "text", "data": {"text": "..."}}]}
```

`content` 为 OneBot V11 段数组，仅支持：

- `text`：`{"type":"text","data":{"text":"..."}}`
- `at`：`{"type":"at","data":{"qq":"10001"}}`
- `reply`：`{"type":"reply","data":{"id":"<message_id>"}}`
- `face`：`{"type":"face","data":{"id":"178"}}`

所有段字段均位于 `data` 内。`reply` 段最多一个；存在时必须是
`content[0]`。空 `content`、空文本和未登记段类型会导致整次调用在发送前失败。

### meme 气泡

```json
{"kind": "meme", "image_hash": "<saved-memes 中的 64 位 sha256>"}
```

`image_hash` 必须存在于当前 `<saved-memes>` 收藏中。每个 meme 气泡
独占一个气泡位置；meme 数量不单独设限，只受 `messages` 总数上限约束。
发送前会统一检查收藏记录与媒体文件；任一项无效时不会发送任何气泡。

组稿时先扫一遍 `<saved-memes>`：描述与当下场面对得上的图，独立成一个
meme 气泡发出，可以代替一句话甚至一整段文字——贴切的图往往比解释更有
表达力。meme 气泡与 chat 气泡按数组顺序自由穿插。`image_hash` 从
`<meme hash="...">` 整段复制即可；复制无误的 hash 不会在发送前校验上
失败。

## 执行与记录

同一次调用中的气泡按顺序逐项发送。每次 `send_messages` 调用都是独立发送
命令；运行时不会合并或去重两次调用。该调用自身
`<tool-call name="send_messages">` 行中的参数与逐气泡回执构成发送记录，不会
另外生成当前链路的 `<my-reply>` 行。

## 返回状态

返回值包含 `status` 和逐气泡 `receipts`：

- `sent`：全部气泡确认送达，工具终态为成功。
- `partial`：部分气泡确认送达、其余明确失败，工具终态为失败。回执中
  `status="sent"` 的气泡已经送达。
- `failed`：全部气泡明确未送达，工具终态为失败。
- `uncertain`：至少一个气泡的送达状态无法确认，工具终态为失败。该状态表示
  对应气泡可能已经送达；再次调用会产生新的独立发送命令，可能形成重复消息。

`invalid_arguments` 会通过 `reason_code` 标识具体参数问题。

# Reply usage — how to write the `reply.content` array

`reply.content` is a list of OneBot V11 message segments. You may MIX as many segment types as you like in one reply. Pick the right segment for QQ-native behaviour — plain text alone often reads as cold/robotic in a group chat.

## Supported segments

### Plain text
```
{"type": "text", "data": {"text": "你好啊"}}
```
Adjacent text segments are fine; QQ will concatenate them. Don't put XML-like `<image hash="..."/>` tags into text — those are only render hints in the timeline, NOT a sendable format.

### @ a specific user
```
{"type": "at", "data": {"qq": "12345"}}
```
- `qq` MUST be the numeric user id (string form is also accepted) of the person you want to ping.
- Source it from the timeline: every incoming `<message sender="昵称(USER_ID)">` exposes the sender's id; `<at user="USER_ID" name="..."/>` segments inside messages expose it too.
- Common pattern when answering a specific user: lead with `at` + a single-space `text(" ")` before your actual text, so the @ chip and your sentence don't collide visually.

### @ everyone (全体成员)
```
{"type": "at", "data": {"qq": "all"}}
```
Use sparingly — most groups consider this rude unless announcing something urgent. The bot also needs admin permission for this to succeed; if it fails, napcat will surface `reply_failed`.

### Quote-reply a specific message (引用回复)
```
{"type": "reply", "data": {"id": "MESSAGE_ID"}}
```
- `id` is the `onebot_message_id` of the message you want to quote. Find it in the timeline as `<message ... id="MESSAGE_ID">`.
- Convention: when present, put the `reply` segment as the FIRST element of `content`. QQ clients render it as a quoted card on top of your reply.
- Quote-reply gives the user a visual anchor when the conversation has scrolled past their message. Prefer it over `at` when the context is more important than pinging.

### QQ-native emoticon (黄豆小表情)
```
{"type": "face", "data": {"id": "178"}}
```
`id` is the QQ face index (small integer; e.g. 178 ≈ slight-smile, 14 ≈ smile, 21 ≈ cute). Use to add tone without spamming emoji unicode. Do not invent ids you don't know — when unsure, just use unicode emoji inside a `text` segment.

## Combining example

To reply to user 99999 by quoting their message MSG_42 and saying "hello, here you go":

```json
{
  "type": "reply",
  "content": [
    {"type": "reply", "data": {"id": "MSG_42"}},
    {"type": "at", "data": {"qq": "99999"}},
    {"type": "text", "data": {"text": " hello, here you go"}}
  ],
  "target": {"kind": "group", "group_id": 100},
  "related_msg_hashes": []
}
```

## Things NOT to put in `reply.content`

- `image`, `forward`, `card`, `voice`, `video`: the bot has no source of these right now; do not synthesize them.
- `<image hash="..."/>` and other angle-bracket tags from the timeline: those are RENDER HINTS for your input, never a sendable segment.
- `at` with `qq` you didn't actually see in the timeline — you'll @ a stranger or a nonexistent id. Cite the id from a recent message.

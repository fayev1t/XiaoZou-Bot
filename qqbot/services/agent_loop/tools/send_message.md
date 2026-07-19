# Tool: send_message

`send_message` sends a message into the current scope's chat (group or private). It is your one and only way to speak — there is no privileged "reply action"; speaking is just calling this tool, and invoking it is an active choice, never a reflex.

> **Naming.** The *tool* is `send_message` (send one message). Do not confuse it with the `{"type":"reply"}` message *segment* inside `content` (which quote-replies one specific message) — the tool and the segment are different things and no longer share the name `reply`.

**Whether to speak at all is a social decision — see §group_chat_rules for that judgment** (short version: most messages aren't for you; opt in only when someone actually addresses you, it's a DM, or you can genuinely answer an open question no one else has touched). This doc covers only the **mechanics**: how to build the `content` segments and `target` once you've decided to speak.

**If you want the chat to actually see some words, those words must be encoded in `arguments.content`.** Text placed in `reasoning`, `note_task_progress.note`, `create_task.description`, `complete_task.result_summary`, `fail_task.reason`, or `idle.reason` does not go out to QQ and does not count as replying.

## Voice（已迁出）

角色卡（小奏人格 + 字打出来的样子 + 特殊关系）已于 2026-07-19 整体迁至
`qqbot/services/agent_loop/prompts/voice.md`，由 Replyer 组稿时独立加载
（见 replyer.py `_load_voice_text`，缺失即组稿失败）。本文件不再承载人格
内容——send_message 工具已下架，仅存段校验参考与历史实现。

## Arguments

```json
{
  "tool_name": "send_message",
  "arguments": {
    "content": [<segment objects>],
    "target": {"kind": "group", "group_id": 100}
  }
}
```

- `content` (required) — an array of OneBot V11 segment objects. See the `content` segment grammar below.
- `target` (required) — `{"kind": "group", "group_id": <int>}` for group scope, or `{"kind": "private", "user_id": <int>}` for private scope. **MUST match the current `<agent-input scope="...">` value**; mismatch returns a `tool_failed` with `error_kind=target_scope_mismatch`.

## `content` segment grammar

Each element in `content` is one OneBot V11 segment. You can MIX as many types as you like; QQ will render them inline in the order given.

**The default message is just plain `text`.** That is how humans talk in a group — they type words and hit send. `at` / `reply` / `face` are *optional* anchors and seasoning for specific situations (spelled out per-segment below), not a checklist to fill: do **not** reflexively open every message with a `{"type":"reply"}` quote — a normal back-and-forth where you answer the latest speaker needs no quote card and no @, and stapling one to every message reads as stiff, bot-like noise.

### Plain text

```json
{"type": "text", "data": {"text": "你好啊"}}
```

Adjacent `text` segments concatenate. Do NOT put XML-like `<image hash="..."/>` strings into text — those are only render hints in the timeline and are not a sendable format.

### @ a specific user

```json
{"type": "at", "data": {"qq": "12345"}}
```

- `qq` MUST be the numeric user id (string form accepted) of the person you want to ping.
- Source the id from the timeline: `<message sender_qq="USER_QQ">` exposes the sender's id, and inline `<at qq="USER_QQ"/>` segments also expose ids — every `*_qq` attribute is a valid value for this field, same id space, copy verbatim.
- Common pattern when answering one user in a busy group: lead with `at` + a single-space `text(" ")` so the @ chip and your text don't collide visually.

### @ everyone (全体成员)

```json
{"type": "at", "data": {"qq": "all"}}
```

Use very sparingly — most groups consider this rude unless announcing something urgent. Requires admin permission; if you lack it the `send_message` tool-call comes back `status="complete"` with an `<error>` — read it and drop the idea.

### Quote-reply a specific message (引用回复) — optional, NOT the default

```json
{"type": "reply", "data": {"id": "MESSAGE_ID"}}
```

- This segment quotes one specific message (QQ renders a quoted card on top of yours). **It is not part of "replying" in general** — answering someone in flowing conversation is plain `text`; you add a `reply` segment only when you deliberately want to *point at one particular message*, typically because:
  - the message you're answering has scrolled several messages up / the topic has moved on, so without an anchor nobody would know what you're responding to; or
  - several parallel threads are running and you must disambiguate which one you're addressing; or
  - you're referring back to something said much earlier.
- If the message you're answering is the latest one or two in the timeline, **do not quote it** — just talk.
- `id` is the `onebot_message_id` of the message you want to quote. Read it from the timeline as `<message ... message_id="MESSAGE_ID">` (the envelope attribute is named `message_id`; this outgoing OneBot segment field is just `id`).
- Format rules (machine-enforced): when present, the `reply` segment must be the **first** element of `content`, and **at most one** is allowed — otherwise the call is rejected with `invalid_arguments` (`reason_code=reply_segment_not_first` / `duplicate_reply_segment`) and nothing is sent.

### QQ-native emoticon (黄豆小表情)【尽量不要用这个qq原生的表情都太丑了】

```json
{"type": "face", "data": {"id": "178"}}
```

`id` is the QQ face index (small integer; e.g. 178 ≈ slight-smile, 14 ≈ smile, 21 ≈ cute). Use to add tone without spamming unicode emoji. Don't invent ids you don't know — fall back to unicode emoji inside a `text` segment when unsure.

## Things NOT to put in `content`

`content` accepts **only four** segment types: `text` / `at` / `reply` / `face`. **Anything else is rejected before sending** — the call comes back `invalid_arguments` with `reason_code=unsupported_segment_type` plus `segment_index=` / `segment_type=` telling you exactly which element was bad, and the message does NOT go out.

- `image`, `forward`, `card`, `voice`, `video`, `file`, `markdown`, `xml`, `json` — the bot has no source of these and they are rejected; do not synthesize them.
- `<image hash="..."/>` and other angle-bracket tags from the timeline — those are RENDER HINTS for your input, never a sendable segment.
- `at` with a `qq` you didn't actually see in the timeline — you'll @ a stranger or nonexistent id. Cite the id from a recent `<message>`. (`qq` must be `"all"` or a positive numeric id, else `invalid_arguments`.)
- An empty message — `content` can't be empty and can't be only blank/whitespace text (`reason_code=content_empty` / `content_all_blank`).

## Examples

**The common case — just talk.** Answering the person who just spoke needs nothing but text:

```json
{
  "tool_name": "send_message",
  "arguments": {
    "content": [
      {"type": "text", "data": {"text": "带伞啦,明天有雨"}}
    ],
    "target": {"kind": "group", "group_id": 100}
  }
}
```

**The anchored case — only when you really need to point at one message.** User 99999 asked something a dozen messages ago and the chat has moved on; quote their MSG_42 so the answer lands:

```json
{
  "tool_name": "send_message",
  "arguments": {
    "content": [
      {"type": "reply", "data": {"id": "MSG_42"}},
      {"type": "at", "data": {"qq": "99999"}},
      {"type": "text", "data": {"text": " hello, here you go"}}
    ],
    "target": {"kind": "group", "group_id": 100}
  }
}
```

## Result

Sending is **synchronous**, like every other tool. On success the result is `{"message_id": <id>, "self_id": "...", "sent": true}` (`self_id` = your own QQ id, the same value as `bot_qq`) and the message has **actually gone out**. In the next tick's timeline a sent message appears as its own `<tool-call name="send_message" status="complete">` row carrying a `<result>` (your words are right there in `<args>` `content`) — there is no separate `<agent-reply>` row. **That row means you have already said this — it is history, never something to re-send.** If sending fails, the tool-call comes back `status="complete"` with an `<error>` instead (e.g. you're muted in this group, or the target is wrong) — the message did NOT go out, so read the reason and decide whether to retry or drop; a `<result>` always means it really sent. When someone later quote-replies you, their message will carry `<reply ... from_self="true"/>`, which is how you recognise a reply aimed at you.

On a `target` that doesn't match the current scope the tool returns `target_scope_mismatch` (with `expected_scope` / `actual_target_kind` / `actual_target_id`); on a bad `content` segment (unknown type, `reply` not first, empty, …) it returns `invalid_arguments` with a `reason_code` pinpointing the problem. A rare `upstream_action_failed` with `reason_code=missing_message_id` means QQ accepted the call but returned no message id, so it doesn't count as sent. Fix the specific problem it names and retry on the next tick.

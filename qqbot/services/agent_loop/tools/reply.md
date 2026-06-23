# Tool: reply

`reply` sends a message into the current scope's chat (group or private). It is your one and only way to speak — there is no privileged "reply action"; speaking is just calling this tool, and invoking it is an active choice, never a reflex.

**Whether to speak at all is a social decision — see §group_chat_rules for that judgment** (short version: most messages aren't for you; opt in only when someone actually addresses you, it's a DM, or you can genuinely answer an open question no one else has touched). This doc covers only the **mechanics**: how to build the `content` segments and `target` once you've decided to speak.

## Arguments

```json
{
  "tool_name": "reply",
  "arguments": {
    "content": [<segment objects>],
    "target": {"kind": "group", "group_id": 100},
    "related_msg_hashes": []
  }
}
```

- `content` (required) — an array of OneBot V11 segment objects. See §reply segment grammar below.
- `target` (required) — `{"kind": "group", "group_id": <int>}` for group scope, or `{"kind": "private", "user_id": <int>}` for private scope. **MUST match the current `<agent-input scope="...">` value**; mismatch returns a `tool_failed` with `error_kind=target_scope_mismatch`.
- `related_msg_hashes` (optional) — list of message hashes this reply relates to; used by downstream bookkeeping, currently informational.

## `content` segment grammar

Each element in `content` is one OneBot V11 segment. You can MIX as many types as you like; QQ will render them inline in the order given. Plain text alone often reads as cold/robotic — prefer mixing `at` / `reply` / `face` when appropriate.

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
- Source the id from the timeline: `<message sender="昵称(USER_ID)">` exposes the sender's id, and inline `<at user="USER_ID"/>` segments also expose ids.
- Common pattern when answering one user in a busy group: lead with `at` + a single-space `text(" ")` so the @ chip and your text don't collide visually.

### @ everyone (全体成员)

```json
{"type": "at", "data": {"qq": "all"}}
```

Use very sparingly — most groups consider this rude unless announcing something urgent. Requires admin permission; if it fails, you'll see `agent.reply_failed` next tick.

### Quote-reply a specific message (引用回复)

```json
{"type": "reply", "data": {"id": "MESSAGE_ID"}}
```

- `id` is the `onebot_message_id` of the message you want to quote. Read it from the timeline as `<message ... id="MESSAGE_ID">`.
- Convention: put the `reply` segment as the FIRST element of `content`. QQ clients render it as a quoted card on top of your message.
- Use this when the conversation has scrolled past the user's message OR when quoting the context is more useful than just pinging. Otherwise prefer `at`.

### QQ-native emoticon (黄豆小表情)【尽量不要用这个qq原生的表情都太丑了】

```json
{"type": "face", "data": {"id": "178"}}
```

`id` is the QQ face index (small integer; e.g. 178 ≈ slight-smile, 14 ≈ smile, 21 ≈ cute). Use to add tone without spamming unicode emoji. Don't invent ids you don't know — fall back to unicode emoji inside a `text` segment when unsure.

## Things NOT to put in `content`

- `image`, `forward`, `card`, `voice`, `video` — the bot has no source of these right now; do not synthesize them.
- `<image hash="..."/>` and other angle-bracket tags from the timeline — those are RENDER HINTS for your input, never a sendable segment.
- `at` with a `qq` you didn't actually see in the timeline — you'll @ a stranger or nonexistent id. Cite the id from a recent `<message>`.

## Combining example

Reply to user 99999 by quoting their message MSG_42 and saying "hello, here you go":

```json
{
  "tool_name": "reply",
  "arguments": {
    "content": [
      {"type": "reply", "data": {"id": "MSG_42"}},
      {"type": "at", "data": {"qq": "99999"}},
      {"type": "text", "data": {"text": " hello, here you go"}}
    ],
    "target": {"kind": "group", "group_id": 100},
    "related_msg_hashes": []
  }
}
```

## Result

On success the tool result is `{"reply_event_id": "<id>", "queued": true}`. Actual delivery happens asynchronously via ReplySendWorker. In the next tick's timeline this reply appears as its own `<tool-call name="reply" status="succeeded">` row (your words are right there in `<args>` `content`) — there is no separate `<agent-reply>` row. Treat that successful tool-call as "already said" and do not re-send. When someone later quote-replies you, their message will carry `<reply ... from="我(<bot_user_id>)"/>`, which is how you recognise a reply aimed at you.

On `target.kind`/`scope` mismatch or other validation failure, the tool returns `tool_failed` with a short `error_message` — fix and retry on the next tick.

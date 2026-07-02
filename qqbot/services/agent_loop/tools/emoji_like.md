# Tool: emoji_like

`emoji_like` reacts to a single message with a QQ emoji (表情回应) in the **current group**, or removes a reaction you previously added. Maps to the OneBot V11 action `set_msg_emoji_like`.

## When to use

This is a light, friendly way to acknowledge a message without sending a full reply — a thumbs-up on a good answer, a laugh on a joke, a heart on something sweet. Prefer it over a text reply when a one-tap reaction says enough and a sentence would just add noise. Use `set: false` to take back a reaction you added earlier (wrong emoji, no longer apt). It does not notify like a reply and carries no moderation weight.

## Arguments

```json
{
  "tool_name": "emoji_like",
  "arguments": {
    "message_id": 123456,
    "emoji_id": "128",
    "set": true
  }
}
```

- `message_id` (required, int) — the `onebot_message_id` of the target message. Read it from the `id` attribute of a `<message ... id="MESSAGE_ID">` row in the timeline (that `id` **is** the `onebot_message_id`). Don't invent ids.
- `emoji_id` (required, number or string) — the QQ emoji / face id to react with.
- `set` (optional, bool, default `true`) — `true` adds the reaction; `false` removes a reaction you previously added.

The target message is located by `message_id` alone — there is **no** `group_id` argument. The tool still confirms you are acting inside the current group's scope, but napcat needs only the message id.

## Permissions

- **Triggering user**: none required. This is a GUEST-level interaction, so any caller can prompt it and you do **not** need to set `triggered_by_event_id`.
- **The bot itself**: no special role needed — the bot does **not** have to be a group admin to react with an emoji.

## Result

On success: `{"ok": true, "message_id": <int>, "emoji_id": <str>, "set": <bool>}`. On a napcat error (invalid emoji id, the message is gone) you get a `tool_failed` with a human-readable reason — read it, explain or move on, do **not** blindly retry the same call.

# Tool: recall

`recall` recalls (deletes) a single message in the **current group**. Maps to the OneBot V11 action `delete_msg`.

## When to use

Use it to take back a message that should no longer be visible: a message you yourself just sent by mistake (wrong content, a duplicate, a malformed reply), or — when a group admin asks — to remove a clearly inappropriate message from someone else (spam, an ad/scam link, abuse). Recalling is a targeted, single-message action; it does not punish the sender (use `ban` or `kick` for that). Don't recall to "win" an argument or to hide ordinary messages you simply dislike.

## Arguments

```json
{
  "tool_name": "recall",
  "arguments": {
    "message_id": 123456
  }
}
```

- `message_id` (required, int) — the `onebot_message_id` of the message to recall. Read it from the `message_id` attribute of a `<message ... message_id="MESSAGE_ID">` row in the timeline (same field name as this argument — copy it verbatim). Don't invent ids.

The target message is located by `message_id` alone — there is **no** `group_id` argument. The tool still confirms you are acting inside the current group's scope, but napcat needs only the message id to recall it.

## Permissions

- **Triggering user**: none required. This is a GUEST-level action, so any caller can prompt it and you do **not** need to set `triggered_by_event_id`.
- **The bot itself**: recalling your OWN message always works. Recalling SOMEONE ELSE's message requires the bot to be a group **admin** (or owner) **and** to outrank the message's author (an admin cannot recall the owner's or another admin's message). The tool **pre-checks** this: if the author is someone else and the bot isn't admin/owner, or the author's role is equal-or-higher, you get a deterministic `permission_denied_bot_role` (with `required_bot_role` / `target_role`) — do **not** retry, just relay that you can't. If the author can't be determined, the tool defers to napcat.

## Result

On success: `{"ok": true, "message_id": <int>}`. On a permission pre-check failure (`permission_denied_bot_role`) or a napcat error (message too old to recall, unknown id) you get a `tool_failed` with a human-readable reason — read it, explain or abort, do **not** blindly retry the same call.

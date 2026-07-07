# Tool: set_essence

`set_essence` marks a single message as a group **essence** message (群精华), or removes it from the essence list. Maps to the OneBot V11 actions `set_essence_msg` (add) and `delete_essence_msg` (remove).

## When to use

Group essence is a curated, group-wide highlight list — pinning something there is high-visibility and semi-permanent, so use it sparingly and only when a group owner asks. Good cases: a genuinely valuable post (a guide, an announcement, a memorable moment) the owner wants kept; or `action: "delete"` to clean out an outdated or mistakenly-added entry. Don't add essence on a whim or for ordinary chatter.

## Arguments

```json
{
  "tool_name": "set_essence",
  "arguments": {
    "message_id": 123456,
    "action": "set"
  }
}
```

- `message_id` (required, int) — the `onebot_message_id` of the target message. Read it from the `message_id` attribute of a `<message ... message_id="MESSAGE_ID">` row in the timeline (same field name as this argument — copy it verbatim). Don't invent ids.
- `action` (optional, string, default `"set"`) — `"set"` adds the message to the group essence list; `"delete"` removes it from the list.

The target message is located by `message_id` alone — there is **no** `group_id` argument. The tool still confirms you are acting inside the current group's scope, but napcat needs only the message id.

## Permissions

- **Triggering user**: this is an OWNER-level action — a group owner must have asked for it. Set `triggered_by_event_id` on the call to that person's message; if you omit it the caller is treated as GUEST and the action is refused.
- **The bot itself** must be a group **admin** (or owner). If it isn't, the call fails and you'll see the reason next tick — relay it, don't keep retrying.

## Result

On success: `{"ok": true, "message_id": <int>, "action": "set"|"delete"}`. On a permission failure (caller not allowed, or the bot isn't admin) or a napcat error you get a `tool_failed` with a human-readable reason — read it, explain or abort, do **not** blindly retry the same call.

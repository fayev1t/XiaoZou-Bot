# Tool: kick

`kick` removes (kicks) a member from the **current group**. Maps to the OneBot V11 action `set_group_kick`.

## When to use

Only to remove a genuinely disruptive member — a spammer, an ad/scam bot, or someone a group admin explicitly asked you to remove. Kicking is heavy and not reversible (the person is gone, though they can rejoin unless you also reject future requests). For a temporary problem prefer `ban` (mute) instead. Don't kick on a whim, on one tense message, or to "win" an argument.

## Arguments

```json
{
  "tool_name": "kick",
  "arguments": {
    "user_id": 12345,
    "reject_add_request": false
  }
}
```

- `user_id` (required, int) — the QQ number of the member to kick. Read it from a `<message sender_qq="USER_QQ">` row in the timeline, or from an inline `<at qq="USER_QQ"/>` segment. Don't invent ids.
- `reject_add_request` (optional, bool, default `false`) — if `true`, also block this user's *future* join requests. Use for persistent ad bots you don't want coming back; leave `false` for ordinary removals.

The target group is **always the current one** — `group_id` is taken from your scope automatically; you cannot kick someone out of another group and there is no `group_id` argument.

## Permissions

- **Triggering user**: this is an ADMIN-level action — a group admin or owner must have asked for it. Set `triggered_by_event_id` on the call to that person's message; if you omit it the caller is treated as GUEST and the kick is refused.
- **The bot itself** must be a group **admin** (or owner). If it isn't, the call fails and you'll see the reason next tick — relay it, don't keep retrying.
- **Role hierarchy** is pre-checked: the bot can only kick someone whose role is **strictly lower** than its own (an admin can kick members but not the owner or another admin; nobody can kick the owner). If the target's role is equal-or-higher, you get a deterministic `permission_denied_bot_role` (with `target_role`) before napcat is touched — don't retry.

## Result

On success: `{"ok": true, "group_id": <int>, "user_id": <int>}`. On a permission failure (caller not allowed, or the bot isn't admin) or a napcat error you get a `tool_failed` with a human-readable reason — read it, explain or abort, do **not** blindly retry the same call.

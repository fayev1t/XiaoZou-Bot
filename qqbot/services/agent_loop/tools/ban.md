# Tool: ban

`ban` mutes (禁言) — or lifts the mute of — a member in the **current group**. Maps to the OneBot V11 action `set_group_ban`.

## When to use

Use it to temporarily silence a genuinely disruptive member: a flooder, someone spamming ads, or a heated user a group admin explicitly asked you to cool down. A mute is reversible and time-bounded, so it is the *preferred* light-touch moderation action — reach for it before `kick`. To release someone early, call again with `duration=0`.

Don't mute on a whim, over a single tense message, or to "win" an argument. Muting an admin or the group owner won't work and just produces an error.

## Arguments

```json
{
  "tool_name": "ban",
  "arguments": {
    "user_id": 12345,
    "duration": 1800
  }
}
```

- `user_id` (required, int) — the QQ number of the member to mute. Read it from a `<message sender_qq="USER_QQ">` row in the timeline, or from an inline `<at qq="USER_QQ"/>` segment. Don't invent ids.
- `duration` (optional, int, default `1800`) — mute length in **seconds** (1800 = 30 minutes). Pass `0` to **lift** an existing mute. Keep it proportionate: minutes for a brief flood, not days.

The target group is **always the current one** — `group_id` is taken from your scope automatically; you cannot mute someone in another group and there is no `group_id` argument.

## Permissions

- **Triggering user**: this is an ADMIN-level action — a group admin or owner must have asked for it. Set `triggered_by_event_id` on the call to that person's message; if you omit it the caller is treated as GUEST and the mute is refused.
- **The bot itself** must be a group **admin** (or owner). If it isn't, the call fails and you'll see the reason next tick — relay it, don't keep retrying.
- **Role hierarchy** is pre-checked: the bot can only mute someone whose role is **strictly lower** than its own (an admin can mute members but not the owner or another admin). If the target's role is equal-or-higher, you get a deterministic `permission_denied_bot_role` (with `target_role`) before napcat is touched — don't retry.

## Result

On success: `{"ok": true, "group_id": <int>, "user_id": <int>, "duration": <int>}`. On a permission failure (caller not allowed, or the bot isn't admin) or a napcat error you get a `tool_failed` with a human-readable reason — read it, explain or abort, do **not** blindly retry the same call.

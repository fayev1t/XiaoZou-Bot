# Tool: set_admin

`set_admin` grants or revokes group-admin (管理员) status for a member of the **current group**. Maps to the OneBot V11 action `set_group_admin`.

## When to use

Use it **only** on the explicit instruction of the group **owner** to promote a trusted member to admin, or to demote one. This hands over (or takes back) real moderation power — the ability to mute, kick, and manage others — so it is one of the most consequential actions available. There is no partial step: the person either becomes an admin or loses it.

Never promote/demote on your own judgement, to reward banter, or based on a non-owner's request. When in doubt, decline and ask the owner to confirm.

## Arguments

```json
{
  "tool_name": "set_admin",
  "arguments": {
    "user_id": 12345,
    "enable": true
  }
}
```

- `user_id` (required, int) — the QQ number of the member to promote or demote. Read it from a `<message sender_qq="USER_QQ">` row in the timeline, or from an inline `<at qq="USER_QQ"/>` segment. Don't invent ids.
- `enable` (optional, bool, default `true`) — `true` makes them a group admin; `false` revokes their admin. Be explicit; don't rely on the default when demoting.

The target group is **always the current one** — `group_id` is taken from your scope automatically; you cannot change roles in another group and there is no `group_id` argument.

## Permissions

- **Triggering user**: this is an OWNER-level action — only the group owner can authorize it. Set `triggered_by_event_id` on the call to the owner's message; if you omit it the caller is treated as GUEST and the change is refused.
- **The bot itself** must be the group **owner** (群主). Admin is not enough — appointing admins is an owner-only power. If the bot isn't the owner, the call fails and you'll see the reason next tick — relay it, don't keep retrying.

## Result

On success: `{"ok": true, "group_id": <int>, "user_id": <int>, "enable": <bool>}`. On a permission failure (caller not the owner, or the bot isn't the owner) or a napcat error you get a `tool_failed` with a human-readable reason — read it, explain or abort, do **not** blindly retry the same call.

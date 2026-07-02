# Tool: leave_group

`leave_group` makes the bot **leave the current group**, or **dismiss (disband) the whole group** if the bot is its owner and you set `is_dismiss=true`. Maps to the OneBot V11 action `set_group_leave`.

## When to use

⚠️ **HIGH-RISK and IRREVERSIBLE.** After this runs the bot is gone from the group: it can no longer receive or send anything there, and this cannot be undone (someone would have to re-invite it). With `is_dismiss=true` and the bot as owner, the **entire group is disbanded** for everyone — even more drastic.

Only ever do this on an **explicit, unambiguous** request from the group owner (or a system admin) to leave or disband — for example they clearly say "退群" / "解散这个群" and mean it. Never leave on your own initiative, never on a vague or joking remark, and if there's any doubt, ask for confirmation instead of calling this tool.

## Arguments

```json
{
  "tool_name": "leave_group",
  "arguments": {
    "is_dismiss": false
  }
}
```

- `is_dismiss` (optional, bool, default `false`) — `false` (default): the bot just leaves the group. `true`: disband the whole group instead of merely leaving — this requires the bot to be the group **owner**. If you pass `is_dismiss=true` while the bot is **not** the owner, the tool rejects the whole call up front with `permission_denied_bot_role` (it does **not** silently fall back to a plain leave); if you actually want the bot to just leave, pass `is_dismiss=false`.

The target group is **always the current one** — `group_id` is taken from your scope automatically; you cannot leave another group and there is no `group_id` argument.

## Permissions

- **Triggering user**: this is an OWNER-level action — the group owner (or a system admin) must have explicitly asked for it. Set `triggered_by_event_id` on the call to that person's message; if you omit it the caller is treated as GUEST and the action is refused.
- **The bot itself**: no admin role is required just to *leave* a group, so there is no static `required_bot_role` gate. But *dismiss* (`is_dismiss=true`) is owner-only and the **tool pre-checks it**: if the bot's `bot_role` isn't `owner`, the call fails with a deterministic `permission_denied_bot_role` (`required_bot_role=owner`) before touching napcat — don't retry.

## Result

On success: `{"ok": true, "group_id": <int>, "is_dismiss": <bool>}` — though once the bot has left you typically won't act further in this group. On a permission failure (caller not allowed) or a napcat error you get a `tool_failed` with a human-readable reason — read it, explain or abort, do **not** blindly retry the same call.

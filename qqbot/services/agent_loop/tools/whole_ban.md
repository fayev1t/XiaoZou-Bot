# Tool: whole_ban

`whole_ban` turns the **current group's** whole-group mute (全员禁言) on or off. Maps to the OneBot V11 action `set_group_whole_ban`.

## When to use

This is a **high-impact** switch: when it is on, *every* ordinary member is silenced and only admins/owner can speak. Reach for it only in genuine group-wide situations a group owner asked you to handle — e.g. shutting down a spam flood or ad raid, or quieting the group during an announcement — and lift it again as soon as the situation is over. Turning it on affects everyone at once and is highly visible, so never do it on a whim or to win an argument; for one disruptive person prefer `ban` (mute that member) or `kick` instead.

## Arguments

```json
{
  "tool_name": "whole_ban",
  "arguments": {
    "enable": true
  }
}
```

- `enable` (optional, bool, default `true`) — `true` turns whole-group mute **on** (nobody but admins can speak); `false` **lifts** it. Be explicit: pass `false` when someone asks you to "unmute the group" / "解除全员禁言".

The target group is **always the current one** — `group_id` is taken from your scope automatically; you cannot mute another group and there is no `group_id` argument.

## Permissions

- **Triggering user**: this is an OWNER-level action — the group owner (or a system admin) must have asked for it. Set `triggered_by_event_id` on the call to that person's message; if you omit it the caller is treated as GUEST and the action is refused.
- **The bot itself** must be a group **admin** (or owner). If it isn't, the call fails and you'll see the reason next tick — relay it, don't keep retrying.

## Result

On success: `{"ok": true, "group_id": <int>, "enable": <bool>}`. On a permission failure (caller not allowed, or the bot isn't admin) or a napcat error you get a `tool_failed` with a human-readable reason — read it, explain or abort, do **not** blindly retry the same call.

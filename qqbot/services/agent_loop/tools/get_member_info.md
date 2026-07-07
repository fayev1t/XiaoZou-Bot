# Tool: get_member_info

`get_member_info` looks up one member's profile in the **current group**. Maps to the OneBot V11 action `get_group_member_info`.

## When to use

This is a **read-only** lookup. Reach for it when you need facts about a specific member that the timeline doesn't already give you: their role (`owner`/`admin`/`member`), their group card / display name, their level or special title, when they joined, or when they last spoke. Typical cases: before doing or relaying an admin action, confirm whether the person who asked is actually an admin/owner; or when someone is referenced and you want their proper card name instead of guessing. Don't call it for someone whose details are already visible in recent messages — prefer what you can already see.

## Arguments

```json
{
  "tool_name": "get_member_info",
  "arguments": {
    "user_id": 12345
  }
}
```

- `user_id` (required, int) — the QQ number of the member to look up. Read it from a `<message sender_qq="USER_QQ">` row in the timeline, or from an inline `<at qq="USER_QQ"/>` segment. Don't invent ids.

The target group is **always the current one** — `group_id` is taken from your scope automatically; you cannot look up a member of another group and there is no `group_id` argument.

## Permissions

- **Triggering user**: none required. This is a GUEST-level read with no side effects, so any caller can prompt it and you do **not** need to set `triggered_by_event_id`.
- **The bot itself**: no special role needed — the bot does **not** have to be a group admin to read member info.

## Result

On success you get the slimmed profile: `{"user_id", "nickname", "card", "role", "level", "title", "join_time", "last_sent_time"}`. Verbose napcat fields (avatar, area, etc.) are dropped to keep your context small. `card` may be empty when the member has no group nickname; `title` may be empty too. If the user isn't in the group, or napcat errors, you get a `tool_failed` with a human-readable reason — read it and explain or move on, do **not** blindly retry the same call.

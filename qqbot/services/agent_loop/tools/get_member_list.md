# Tool: get_member_list

`get_member_list` lists members of the **current group**. Maps to the OneBot V11 action `get_group_member_list`.

## When to use

This is a **read-only** lookup. Use it when you need a roster of who is in the group — for example to count members, to list all the admins (`role: "admin"`), to find someone by name when you only have a partial nickname, or to see who's been active lately (`include_activity: true`). The full list can be hundreds or thousands of entries, so the result is **truncated** (see Arguments / Result); it is meant for sampling, filtering and counting, not for dumping every member into your reply. If you only need one person's details, prefer `get_member_info` instead.

## Arguments

```json
{
  "tool_name": "get_member_list",
  "arguments": {
    "limit": 200,
    "role": "admin",
    "include_activity": false
  }
}
```

- `limit` (optional, int, default `200`, capped at `500`) — the maximum number of member entries to return. Values above 500 are clamped to 500; the *total* member count is still reported separately as `count` regardless of this cap. Omit it to use the default of 200.
- `role` (optional, one of `"owner"` / `"admin"` / `"member"`) — only return members with this group role. The filter applies **before** truncation, so `role: "admin"` reliably returns every admin even in a huge group.
- `include_activity` (optional, bool, default `false`) — when true, each entry also carries `join_time` and `last_sent_time` as Asia/Shanghai ISO timestamps. This costs tokens; only enable it when activity actually matters (e.g. "谁最近都不说话？").

The target group is **always the current one** — `group_id` is taken from your scope automatically; you cannot list another group and there is no `group_id` argument.

## Permissions

- **Triggering user**: none required. This is a GUEST-level read with no side effects, so any caller can prompt it and you do **not** need to set `triggered_by_event_id`.
- **The bot itself**: no special role needed — the bot does **not** have to be a group admin to read the member list.

## Result

On success: `{"count": <int>, "matched": <int>, "members": [ {"user_id", "nickname", "card", "role"}, ... ]}`.

- `count` is the *full* member total of the group (unfiltered); `matched` is how many members pass the `role` filter (equal to `count` when no filter is given).
- `members` is the (filtered) list **truncated to `limit`**, with each entry slimmed to user_id / nickname / card / role (level, avatar, etc. are dropped). A member who is **currently muted** additionally carries `banned_until` (Asia/Shanghai ISO); the key is absent for everyone else. With `include_activity: true` each entry also has `join_time` / `last_sent_time` (ISO, may be null).
- If `matched` is larger than `len(members)`, you only have a partial view — don't claim it's the whole roster.

On a napcat error you get a `tool_failed` with a human-readable reason — read it and explain or move on, do **not** blindly retry the same call.

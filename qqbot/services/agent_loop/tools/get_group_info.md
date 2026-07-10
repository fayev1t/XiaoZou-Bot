# Tool: get_group_info

`get_group_info` gets basic info about the **current group**. Maps to the OneBot V11 action `get_group_info`.

## When to use

This is a **read-only** lookup. Use it when you need the group's own facts rather than a member's: the group's name, how many members it has now, or its capacity (max members). Typical cases: someone asks "how many people are in here?", "what's this group called?", or "are we close to full?". Don't call it for per-member details — use `get_member_info` / `get_member_list` for those.

## Arguments

```json
{
  "tool_name": "get_group_info",
  "arguments": {}
}
```

This tool **takes no arguments**. The target group is **always the current one** — `group_id` is taken from your scope automatically; you cannot query another group and there is no `group_id` argument.

## Permissions

- **Triggering user**: none required. This is a GUEST-level read with no side effects, so any caller can prompt it and you do **not** need to set `triggered_by_event_id`.
- **The bot itself**: no special role needed — the bot does **not** have to be a group admin to read group info.

## Result

On success: `{"group_id", "group_name", "member_count", "max_member_count"}`, plus two **optional** keys that appear only when the platform actually provides them: `group_remark` (the group's remark/memo) and `group_create_time` (when the group was created, as an Asia/Shanghai ISO timestamp). Don't be surprised when they're absent — many napcat versions simply don't return them. Other verbose napcat fields (group_level, etc.) are dropped to keep your context small. `member_count` is the live count (fetched with no_cache) and `max_member_count` is the group's capacity. On a napcat error you get a `tool_failed` with a human-readable reason — read it and explain or move on, do **not** blindly retry the same call.

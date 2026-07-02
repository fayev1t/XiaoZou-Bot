# Tool: get_group_honor

`get_group_honor` gets the honor / leaderboard info for the **current group** (talkative streak 龙王, performers 群聊之火, legends 群聊炽焰, etc.). Maps to the OneBot V11 action `get_group_honor_info`.

## When to use

This is a **read-only** lookup. Use it when the conversation is about group rankings or honors: who the current 龙王 (talkative streak holder) is, who the top talkers / performers are, or for a bit of fun when someone asks "谁是龙王". Each leaderboard can be long, so the result keeps only the **top few** of each list — it's for naming the leaders, not for reproducing the full board.

## Arguments

```json
{
  "tool_name": "get_group_honor",
  "arguments": {
    "type": "all"
  }
}
```

- `type` (optional, string, default `"all"`) — which leaderboard to fetch. One of: `talkative`, `performer`, `legend`, `strong_newbie`, `emotion`, or `all`. Use `all` (the default) to get every board at once; pick a specific one only when you just need that single ranking.

The target group is **always the current one** — `group_id` is taken from your scope automatically; you cannot query another group and there is no `group_id` argument.

## Permissions

- **Triggering user**: none required. This is a GUEST-level read with no side effects, so any caller can prompt it and you do **not** need to set `triggered_by_event_id`.
- **The bot itself**: no special role needed — the bot does **not** have to be a group admin to read honor info.

## Result

On success: `{"group_id", "type", ...}` plus, when present, `current_talkative` (the active 龙王) and one or more `*_list` entries (`talkative_list`, `performer_list`, `legend_list`, …). Each leaderboard list is **trimmed to its top 5** entries, and every entry is slimmed to `{"user_id", "nickname", "description"}` (avatars and counts are dropped). A board with no data simply won't appear in the result. On a napcat error you get a `tool_failed` with a human-readable reason — read it and explain or move on, do **not** blindly retry the same call.

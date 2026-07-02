# Tool: get_stranger_info

`get_stranger_info` looks up the basic **public** profile of any QQ user by their QQ number, even someone who is not in this group. Maps to the OneBot V11 action `get_stranger_info`.

## When to use

This is a **read-only** lookup. Use it when you need the public profile of a QQ account that you can't get from group membership — for example someone mentioned by number who isn't in the current group, or a join applicant you want to glance at. It reads QQ's public stranger info, so it does **not** depend on a group at all and works in any scope. If the person *is* in the current group and you need their group-specific details (card, role, title), prefer `get_member_info` instead.

## Arguments

```json
{
  "tool_name": "get_stranger_info",
  "arguments": {
    "user_id": 12345
  }
}
```

- `user_id` (required, int) — the QQ number of the user to look up. Read it from a `<message sender_id="USER_ID">` row, an inline `<at user="USER_ID"/>` segment, or wherever the number was given. Don't invent ids.

Unlike the group lookups, there is **no group scope** here — this tool does not take or use a `group_id` and can run in any scope.

## Permissions

- **Triggering user**: none required. This is a GUEST-level read with no side effects, so any caller can prompt it and you do **not** need to set `triggered_by_event_id`.
- **The bot itself**: no special role needed — the bot does **not** have to be a group admin to read public stranger info.

## Result

On success: `{"user_id", "nickname", "sex", "age"}`. Verbose napcat fields (qid, level, etc.) are dropped to keep your context small. `sex` may be `"unknown"` and `age` may be `0` when the user hasn't set them publicly. If the QQ number can't be resolved, or napcat errors, you get a `tool_failed` with a human-readable reason — read it and explain or move on, do **not** blindly retry the same call.

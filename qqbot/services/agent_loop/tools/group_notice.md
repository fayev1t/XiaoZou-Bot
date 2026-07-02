# Tool: group_notice

`group_notice` publishes a group notice (群公告) in the **current group**. Maps to the OneBot V11 extended action `_send_group_notice` — an underscore-prefixed action, so it is invoked through `bot.call_api` rather than a generated method.

## When to use

Only for a real, group-wide announcement the owner wants everyone to see — rule changes, event notices, important reminders. A group notice is a high-visibility broadcast shown to the whole group, so it is heavy and intrusive; don't use it for ordinary chatter, casual replies, or anything a normal message would handle. Prefer `send_message` for everyday talk and reserve `group_notice` for things that genuinely warrant a formal notice.

## Arguments

```json
{
  "tool_name": "group_notice",
  "arguments": {
    "content": "群规更新：禁止刷屏，违者禁言。",
    "image": "http://example.com/banner.png"
  }
}
```

- `content` (required, string, non-empty) — the notice body text. Whitespace-only content is rejected.
- `image` (optional, string) — a URL or file path of an image to attach to the notice. Omit it for a text-only notice.

The target group is **always the current one** — `group_id` is taken from your scope automatically; you cannot post a notice to another group and there is no `group_id` argument.

## Permissions

- **Triggering user**: this is an OWNER-level action — the group owner must have asked for it. Set `triggered_by_event_id` on the call to that person's message; if you omit it the caller is treated as GUEST and the notice is refused.
- **The bot itself** must be a group **admin** (or owner). If it isn't, the call fails and you'll see the reason next tick — relay it, don't keep retrying.

## Result

On success: `{"ok": true, "group_id": <int>}`. On a permission failure (caller not allowed, or the bot isn't admin) or a napcat error you get a `tool_failed` with a human-readable reason — read it, explain or abort, do **not** blindly retry the same call.

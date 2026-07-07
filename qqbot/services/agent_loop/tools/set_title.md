# Tool: set_title

`set_title` sets — or clears — the special title (专属头衔) of a member in the **current group**. Maps to the OneBot V11 action `set_group_special_title` (note: napcat's parameter is named `special_title`).

## When to use

Use it only when the group **owner** asks you to award a special title — a small honorific badge shown next to a member's name — or to remove one. Titles are cosmetic but owner-gated by QQ, so treat them as a privileged, owner-only favour. To remove a title, pass an empty string.

Don't hand out titles on your own initiative, to tease someone, or on a non-owner's say-so. Keep the text short and inoffensive; QQ caps the length (commonly ~6 Chinese chars) and may silently truncate or reject an over-long title.

## Arguments

```json
{
  "tool_name": "set_title",
  "arguments": {
    "user_id": 12345,
    "title": "传奇人物"
  }
}
```

- `user_id` (required, int) — the QQ number of the member whose title to set. Read it from a `<message sender_qq="USER_QQ">` row in the timeline, or from an inline `<at qq="USER_QQ"/>` segment. Don't invent ids.
- `title` (optional, string, default `""`) — the new special title. An **empty string clears** it. The tool maps this onto napcat's `special_title` field for you; you always pass `title`.

The target group is **always the current one** — `group_id` is taken from your scope automatically; you cannot edit titles in another group and there is no `group_id` argument.

## Permissions

- **Triggering user**: this is an OWNER-level action — only the group owner can authorize it. Set `triggered_by_event_id` on the call to the owner's message; if you omit it the caller is treated as GUEST and the change is refused.
- **The bot itself** must be the group **owner** (群主). Admin is not enough — special titles are an owner-only power. If the bot isn't the owner, the call fails and you'll see the reason next tick — relay it, don't keep retrying.

## Result

On success: `{"ok": true, "group_id": <int>, "user_id": <int>, "title": <str>}`. On a permission failure (caller not the owner, or the bot isn't the owner) or a napcat error you get a `tool_failed` with a human-readable reason — read it, explain or abort, do **not** blindly retry the same call.

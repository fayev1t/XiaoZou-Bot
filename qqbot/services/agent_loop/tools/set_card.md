# Tool: set_card

`set_card` sets — or clears — the group nickname / card (群名片) of a member in the **current group**. Maps to the OneBot V11 action `set_group_card`.

## When to use

Use it only when a group admin or owner asks you to tidy up someone's display name: enforcing a naming convention (e.g. real-name / department tags), fixing an offensive or misleading card, or restoring a name on request. To wipe a card back to the member's default nickname, pass an empty string.

This rewrites how another person is shown to the whole group, so treat it as a sensitive moderation action. Don't change someone's card for fun, to mock them, or without a clear ask — it is intrusive even though it isn't destructive.

## Arguments

```json
{
  "tool_name": "set_card",
  "arguments": {
    "user_id": 12345,
    "card": "新名片"
  }
}
```

- `user_id` (required, int) — the QQ number of the member whose card to set. Read it from a `<message sender_id="USER_ID">` row in the timeline, or from an inline `<at user="USER_ID"/>` segment. Don't invent ids.
- `card` (optional, string, default `""`) — the new display name. An **empty string clears** the card (the member falls back to their QQ nickname). Keep it civil and within QQ's length limits; an over-long card may be rejected by napcat.

The target group is **always the current one** — `group_id` is taken from your scope automatically; you cannot edit cards in another group and there is no `group_id` argument.

## Permissions

- **Triggering user**: this is an ADMIN-level action — a group admin or owner must have asked for it. Set `triggered_by_event_id` on the call to that person's message; if you omit it the caller is treated as GUEST and the change is refused.
- **The bot itself** must be a group **admin** (or owner). If it isn't, the call fails and you'll see the reason next tick — relay it, don't keep retrying.
- **Role hierarchy** is pre-checked: to edit **someone else's** card the bot must outrank them (an admin can edit members' cards but not the owner's or another admin's). Editing the bot's **own** card is always allowed. An equal-or-higher target gives a deterministic `permission_denied_bot_role` (with `target_role`) before napcat is touched — don't retry.

## Result

On success: `{"ok": true, "group_id": <int>, "user_id": <int>, "card": <str>}`. On a permission failure (caller not allowed, or the bot isn't admin) or a napcat error you get a `tool_failed` with a human-readable reason — read it, explain or abort, do **not** blindly retry the same call.

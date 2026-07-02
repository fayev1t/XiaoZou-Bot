# Tool: poke

`poke` sends a poke (戳一戳) to a member in the **current group**. Maps to the OneBot V11 action `group_poke`.

## When to use

A light, harmless nudge — use it to playfully get someone's attention, react to being mentioned, or add a bit of warmth to the chat. It shows up as the standard QQ poke animation for that member. Cheap and friendly, so it's fine for ordinary banter. Don't spam it (repeated pokes are annoying), and don't keep poking someone who has asked to be left alone.

## Arguments

```json
{
  "tool_name": "poke",
  "arguments": {
    "user_id": 12345
  }
}
```

- `user_id` (required, int) — the QQ number of the member to poke. Read it from a `<message sender_id="USER_ID">` row in the timeline, or from an inline `<at user="USER_ID"/>` segment. Don't invent ids.

The target group is **always the current one** — `group_id` is taken from your scope automatically; you cannot poke someone in another group and there is no `group_id` argument.

## Permissions

- **Triggering user**: this is an ordinary GUEST-level action — any group member can prompt it; no special tier is required, so you don't need a `triggered_by_event_id` for permission's sake.
- **The bot itself**: no admin role is needed; a regular member can poke.

## Result

On success: `{"ok": true, "group_id": <int>, "user_id": <int>}`. On a napcat error (e.g. the target isn't in the group) you get a `tool_failed` with a human-readable reason — read it, explain or abort, do **not** blindly retry the same call.

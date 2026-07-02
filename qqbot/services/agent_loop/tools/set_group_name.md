# Tool: set_group_name

`set_group_name` renames the **current group**. Maps to the OneBot V11 action `set_group_name`.

## When to use

The group name is shown to **everyone** in the group and in their group list, so renaming it is a high-visibility change. Only do it when the group owner (or a system admin) explicitly asks for a specific new name — e.g. rebranding the group or fixing a typo they pointed out. Use the exact name they gave; don't invent or "improve" it, and don't rename on your own initiative.

## Arguments

```json
{
  "tool_name": "set_group_name",
  "arguments": {
    "name": "新的群名称"
  }
}
```

- `name` (required, string, non-empty) — the new group name to set. Use exactly what the requester asked for. An empty or whitespace-only value is rejected.

The target group is **always the current one** — `group_id` is taken from your scope automatically; you cannot rename another group and there is no `group_id` argument. (Note: napcat's underlying parameter is `group_name`; you only ever pass `name`.)

## Permissions

- **Triggering user**: this is an OWNER-level action — the group owner (or a system admin) must have asked for it. Set `triggered_by_event_id` on the call to that person's message; if you omit it the caller is treated as GUEST and the action is refused.
- **The bot itself** must be a group **admin** (or owner). If it isn't, the call fails and you'll see the reason next tick — relay it, don't keep retrying.

## Result

On success: `{"ok": true, "group_id": <int>, "group_name": <str>}`. On a permission failure (caller not allowed, or the bot isn't admin) or a napcat error you get a `tool_failed` with a human-readable reason — read it, explain or abort, do **not** blindly retry the same call.

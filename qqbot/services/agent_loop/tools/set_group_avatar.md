# Tool: set_group_avatar

`set_group_avatar` sets the avatar (portrait) of the **current group**. Maps to the OneBot V11 action `set_group_portrait`.

## When to use

The group avatar is shown to **everyone** in the group and in their group list, so changing it is a high-visibility change. Only do it when the group owner (or a system admin) explicitly asks, **and** you actually have a concrete image source to use. NOTE: the bot usually has no ready image of its own (it can't generate a URL or base64 out of thin air), so in practice this tool is rarely usable — if you have no real `file` source, don't call it; say you need an image instead.

## Arguments

```json
{
  "tool_name": "set_group_avatar",
  "arguments": {
    "file": "https://example.com/avatar.png"
  }
}
```

- `file` (required, string, non-empty) — the image source: an HTTP(S) URL, a local file path, or a base64 string. Must point at a real image; an empty or whitespace-only value is rejected. Don't fabricate a URL.

The target group is **always the current one** — `group_id` is taken from your scope automatically; you cannot change another group's avatar and there is no `group_id` argument.

## Permissions

- **Triggering user**: this is an OWNER-level action — the group owner (or a system admin) must have asked for it. Set `triggered_by_event_id` on the call to that person's message; if you omit it the caller is treated as GUEST and the action is refused.
- **The bot itself** must be a group **admin** (or owner). If it isn't, the call fails and you'll see the reason next tick — relay it, don't keep retrying.

## Result

On success: `{"ok": true, "group_id": <int>}`. On a permission failure (caller not allowed, or the bot isn't admin) or a napcat error (e.g. the image source couldn't be fetched) you get a `tool_failed` with a human-readable reason — read it, explain or abort, do **not** blindly retry the same call.

# Tool: respond_to_group_join_request

`respond_to_group_join_request` approves or rejects a pending **join request to the current group** — a `<request kind="group.add" event_id="..." user_qq="..." comment="..."/>` row in the timeline. Maps to the OneBot V11 action `set_group_add_request`.

## When to use

Only when a group **admin or owner** has given an explicit, unambiguous instruction to admit or reject a specific applicant. "同意 123456 进群" / "把刚申请的那个拒了" (with exactly one pending request) are actionable; silence, a joke, or a member (non-admin) saying "让他进吧" are not. When a request appears you may post one short line telling admins it exists — the tool call itself waits for their word.

Ambiguity blocks the call: if "同意他" could match more than one pending `<request>` row, or you cannot tell which applicant is meant, ask for clarification instead of guessing. Never approve or reject on your own judgment, and never respond to the same request twice — after a successful call for an event_id, that request is settled.

This tool cannot process friend requests or group invitations; those never appear in the group timeline.

## Arguments

```json
{
  "tool_name": "respond_to_group_join_request",
  "arguments": {
    "request_event_id": "EV_123",
    "approve": true,
    "reason": ""
  }
}
```

- `request_event_id` (required, string) — the `event_id` attribute of the `<request kind="group.add">` row, copied verbatim. The napcat credential (`flag`) is looked up from that event server-side; you never see or pass it. An event_id that is not a group.add request, or belongs to another group, is refused.
- `approve` (required, boolean) — `true` admits the applicant, `false` turns them away.
- `reason` (optional, string) — shown to the applicant when rejecting; ignored when approving.

## Permissions

- **Triggering user**: ADMIN-level — a group admin or owner must have asked for it. Set `triggered_by_event_id` on the call to that person's `<message>`; their live group role is verified at call time. Omitting it, or pointing at a non-admin's message, yields `permission_denied_user_tier`.
- **The bot itself** must be a group **admin** (or owner) for napcat to accept the response. If it isn't, the call fails with the reason next tick — relay it, don't retry.

## Result

On success: `{"request_event_id": "...", "group_id": <int>, "user_id": <int>, "approve": <bool>, "applied": true}`. The applicant joining also produces a `<notice kind="group_increase">` shortly after an approval. On failure you get a `tool_failed` with a structured reason (bad event_id, wrong group, permission, or a napcat error such as an already-expired request) — read it and explain or move on; don't blindly redial.

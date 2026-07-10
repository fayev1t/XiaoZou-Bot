# Tool: get_pending_join_requests

`get_pending_join_requests` counts and lists the join requests currently **pending** for the **current group**. Maps to the napcat extended action `get_group_system_msg` (go-cqhttp compatible).

## When to use

This is a **read-only** lookup. Use it when an admin asks things like "现在有几个入群申请？" / "谁在申请进群？", or when you want to double-check the live backlog before or after handling a `<request kind="group.add"/>` row. It reflects napcat's **current** view of the account's system messages, filtered down to this group — requests belonging to other groups and invitations for the bot itself are dropped before you see anything.

## Arguments

```json
{
  "tool_name": "get_pending_join_requests",
  "arguments": {}
}
```

This tool **takes no arguments**. The target group is **always the current one** — `group_id` is taken from your scope automatically; you cannot query another group's requests.

## Permissions

- **Triggering user**: none required. This is a GUEST-level read with no side effects, so any caller can prompt it and you do **not** need to set `triggered_by_event_id`. (Actually approving/rejecting via `respond_to_group_join_request` is a separate, ADMIN-gated step.)
- **The bot itself**: must be a **group admin or owner** — QQ only delivers join requests to admins, so a non-admin bot has nothing to query. If the bot isn't admin the call fails upfront with `permission_denied_bot_role` instead of returning a misleading empty list.

## Result

On success:

```json
{
  "group_id": 100,
  "pending_count": 2,
  "requests": [
    {"user_id": 456, "nickname": "小明", "comment": "同学推荐来的"}
  ],
  "handled_recent_count": 1,
  "may_be_incomplete": true
}
```

- `requests` — **pending** requests only, capped at 50 entries (`pending_count` is the full filtered count). `nickname` / `comment` may be null. The napcat `flag` credential is **never** included — approving never requires it from you.
- `handled_recent_count` — recently handled (already accepted/rejected) requests of this group still visible in the system-message window. Informational only; do not act on them again.
- `may_be_incomplete` — always true: the platform only returns the most recent system messages, so under a large backlog the true count may be higher. When precision matters, say "至少 N 个" instead of claiming an exact total.

## Acting on a request

This tool does **not** return a `request_event_id`. To approve or reject, find the matching `<request kind="group.add" user_qq="..."/>` row in your timeline (match by `user_id`) and call `respond_to_group_join_request` with that row's `event_id` — and only on an explicit admin/owner instruction. If a pending request has **no** timeline row (it arrived while the bot was offline and napcat does not re-push request events), you cannot respond to it via tools: tell the admin to handle that one in the QQ client. On a napcat error you get a `tool_failed` with a human-readable reason — read it and explain or move on, do **not** blindly retry the same call.

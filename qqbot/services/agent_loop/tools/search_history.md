# search_history — when and how to use

## When to call

The default `timeline` shows you only the most recent ~100 events in this scope. Reach for `search_history` when:

- A user references something that happened "前天" / "上周" / "昨晚那个 XX" and you don't see it in the current timeline.
- You're in a long-running task and need to recall what was said when the task was first created (use `task_id` to resolve the anchor automatically).
- You need to verify whether a topic / question has been asked before in this group.

Do **NOT** call this when the answer is already visible in the timeline or `pending_tool_results`.

## Arguments — filters combine with AND

Pick the smallest filter set that gets the job done. Three filter dimensions, all optional but at least one is required:

- `anchor_event_id` (string): return events strictly OLDER than this event_id. Use when you already know an exact anchor (e.g. from a message id in the timeline).
- `task_id` (string): if you have a `task_id` but no anchor_event_id, the tool will look up that task's `triggered_by_event_id` and use it as the anchor. Convenient for "what's the context around when I created this task".
- `start_time` / `end_time` (ISO8601 strings): bound the search to a time window.
- `query` (string): case-insensitive substring match against message text. Use a short, distinctive keyword — Chinese works fine.
- `limit` (int, default 20, max 50): result cap.

## Result format

Same XML envelope as the normal timeline (`<message ...>`, `<notice ... />`, etc.) — easy to read alongside the live context. Empty list means nothing matched.

## After the call

The result lands in `pending_tool_results`. If it answers your current task, proceed to `reply` / `complete_task`. If it surfaced more questions, you may chain another `search_history` with tightened filters, but avoid loops — three searches without progress means the information probably isn't in the DB.

# Tool: reply

`reply` is the only ordinary chat-speaking entry point. Each call appends **one
self-contained authorization** to the current scope's short-lived draft; a
successful result means **pending**, not sent. Only a later
`<my-reply status="sent|partial">` records words/images that really reached QQ.

**Every call stands alone.** You never pass a task id, never copy a revision,
and nothing you wrote before is merged into what you write now. Say what you
want said *as of this tick*, and how long to hold it. When you call again on a
later tick, that new call is simply the newer authorization.

`action="upsert"` appends. `action="cancel"` withdraws the pending draft
entirely (`reply_task_id` optional — there is at most one).

## What happens to several authorizations

The scope holds one pending draft. Your calls stack up on it as
`<tool-call name="reply">` rows in the timeline, and at flush time the Replyer
reads them in order together with the live conversation.

- **The newest row wins.** Changed your read of the situation, corrected a fact,
  decided a target should not be answered after all? Just say so in a new call —
  the Replyer follows the latest and drops what it contradicts.
- Earlier rows still count for anything the newest doesn't contradict — an extra
  fact, an additional person being answered.
- Because nothing is merged, **there is no way to "remove" one field**. To
  reverse something, state the corrected version; to abandon the whole draft,
  `cancel`.
- Don't re-authorize with nothing new to add. A tick that starts is not a reason
  to call again.

## hold_seconds — how long to hold it

**Required on upsert; there is no default.** It is the wait before the draft is
composed and sent, and the **newest call replaces it outright** — later or
earlier, your call.

You are choosing it from what the tail of the timeline looks like:

| The person looks like they are… | hold_seconds |
|---|---|
| mid-sentence — half a line, "我想问一下", clearly not finished | ~15 |
| pasting something long in parts, several messages back to back | ~30 |
| done — a complete question that has been sitting there | ~8 |
| not waiting on anyone — next installment of an agreed multi-part answer, or `verbatim` | 0 |

These are starting points, not rules; pick what the moment actually calls for.
Maximum is 90, and the draft's hard deadline is fixed 90s from when it was first
created — holding again never pushes that back.

To work out where you stand, subtract what you can already see:
`<current now>` minus the row's `time=` is how long you have been holding;
`<result>.flush_at` minus `<current now>` is how long is left.

## Compose arguments

```json
{
  "action": "upsert",
  "mode": "compose",
  "targets": [
    {
      "message_id": "MSG_42",
      "sender_qq": "12345",
      "context": "李四在和张三约火锅的间隙 @了你，单独问明天天气",
      "guidance": "直接给结论并提醒带伞；火锅那条线不是问你的，别接"
    }
  ],
  "gist": {
    "situation": "群里两条线：张三/李四约火锅；李四另起一问指名问你天气",
    "intent": "回答天气问题",
    "facts": ["tomorrow has rain"],
    "avoid": ["do not invent a temperature"],
    "tone": "brief"
  },
  "hold_seconds": 8
}
```

For `mode="compose"`, describe the conversation and how to respond — never final
lines, and never a content relay. The Replyer reads the same timeline you do, at
the same fidelity; what it cannot do is run your analysis again. So your job in
`targets`/`gist` is the *read*: who is talking to whom, which thread you are
entering, what the message means, and how to answer it.

`targets` says **which thread you are entering**, not the only lines that may be
addressed. Material arriving on that same thread while the draft is held is the
Replyer's to fold in — it can see it and you could not. The Replyer decides
wording, number/order of messages, quote/@ segments, and whether/which saved
meme to use.

Copy `message_id` and `sender_qq` from the timeline. Per target, `context`
(required) is your read of the thread; `guidance` (optional) is angle, approach,
boundaries. Neither is final wording, and neither should re-narrate content the
timeline already shows. `gist.situation` maps the room's concurrent threads,
`gist.intent` is the overall purpose, `facts` must remain true, `avoid` must not
surface, and `tone` is only a light composition hint.

One flush emits at most 4 bubbles (at most one of them a meme). Output the user
explicitly wants in more parts than one flush can carry is a cross-tick task:
keep that task open, authorize only the current installment, and on the wake
that follows each flush (the `<my-reply>` row is already in the timeline)
authorize the next installment — without waiting to be prompted — until the task
completes.

## Verbatim and cancel

Use verbatim only for exact fixed wording or an explicit Replyer-failure escape:

```json
{
  "action": "upsert",
  "mode": "verbatim",
  "verbatim_messages": [
    {"content": [{"type": "text", "data": {"text": "exact text"}}]}
  ],
  "hold_seconds": 0
}
```

It accepts 1-4 messages using the legacy strict `text` / `at` / `reply` / `face`
segment grammar and bypasses the Replyer entirely. A verbatim draft is therefore
**exclusive**: while one is pending, further `upsert` calls fail with
`reply_task_locked` — cancel it first if you changed your mind.

```json
{"action":"cancel"}
```

Cancel takes no arguments; pass `reply_task_id` only if you want to assert
*which* draft you are withdrawing.

Never treat `<tool-call name="reply"><result>...</result>` as speech — it is a
recorded intention. Success returns `reply_task_id`, `revision`, `state`,
`flush_at` and `hard_deadline`; it never returns a sent `message_id`.

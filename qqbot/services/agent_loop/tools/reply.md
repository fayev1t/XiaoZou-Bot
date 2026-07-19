# Tool: reply

`reply` is the only ordinary chat-speaking entry point. It stores or merges a
short-lived `reply_task`; a successful tool result means **pending**, not sent.
Only a later `<my-reply status="sent|partial">` records words/images that really
reached QQ.

Use `action="upsert"` without an id to create. If `<pending-reply>` exists, pass
its `reply_task_id` and `revision` as `expected_revision` to merge new targets or
facts. Merging is what postpones the flush; a new message alone never does.
Use `action="cancel"` with the same id/revision to withdraw it.

For normal `mode="compose"`, provide semantic targets and gist, not final lines.
The Replyer later decides wording, number/order of messages, quote/@ segments,
and whether/which saved meme to use. `mode="verbatim"` carries exact validated
OneBot `content` messages and bypasses the Replyer; it cannot be merged.

Never treat `<tool-call name="reply"><result>...</result>` as speech. When an
open task needs no change, do not upsert it again merely because another tick
started.

## Compose arguments

Create one semantic draft (default `hold_seconds` is `0`, maximum is `90`):

```json
{
  "action": "upsert",
  "mode": "compose",
  "targets": [
    {
      "message_id": "MSG_42",
      "sender_qq": "12345",
      "points": ["answer the weather question", "mention an umbrella"]
    }
  ],
  "gist": {
    "intent": "give the useful conclusion",
    "facts": ["tomorrow has rain"],
    "avoid": ["do not invent a temperature"],
    "tone": "brief"
  },
  "hold_seconds": 8
}
```

`targets` authorizes who/what is being answered. Copy `message_id` and
`sender_qq` from the timeline; `points` are semantic obligations, not final
sentences. `gist.intent` is the overall purpose, `facts` must remain true,
`avoid` must not surface, and `tone` is only a light composition hint.

To merge, copy the exact pending identity and add only genuinely new material:

```json
{
  "action": "upsert",
  "reply_task_id": "R...",
  "expected_revision": 2,
  "mode": "compose",
  "targets": [{"message_id": "MSG_43", "points": ["include the follow-up"]}],
  "gist": {"facts": ["new verified fact"]},
  "hold_seconds": 8
}
```

The merge keeps old targets/gist, de-duplicates list fields and only moves
`flush_at` later, capped by the original `hard_deadline`.

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

It accepts 1-4 messages using the legacy strict `text` / `at` / `reply` /
`face` segment grammar and cannot be merged. Cancel an open task with:

```json
{"action":"cancel","reply_task_id":"R...","expected_revision":2}
```

Success returns `reply_task_id`, `revision`, `state`, `flush_at` and
`hard_deadline`; it never returns a sent `message_id`.

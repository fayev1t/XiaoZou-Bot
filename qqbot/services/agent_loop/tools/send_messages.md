# Tool: send_messages

`send_messages` is how your words actually reach the group. One call sends
1–4 ordered bubbles; nothing else you write anywhere becomes visible chat.

```json
{
  "messages": [
    {
      "kind": "chat",
      "content": [
        {"type": "text", "data": {"text": "周日会提前关门，最好五点前到"}}
      ]
    },
    {"kind": "meme", "image_hash": "<64-char sha256 from <saved-memes>>"}
  ]
}
```

`messages` is the only argument. The target is always the current group —
there is no target field, and you cannot send into another scope.

## Bubbles

- 1–4 bubbles per call, in order; at most **one** of them a meme.
- A complete utterance belongs in **one** call's bubbles. Do not split one
  intent across several `send_messages` calls — every call is a separate send
  command and the runtime never merges or deduplicates two commands.
- `kind:"chat"` content allows OneBot v11 `text` / `at` / `reply` / `face`
  segments only, every field inside `"data"`:
  `{"type":"text","data":{"text":"..."}}` /
  `{"type":"at","data":{"qq":"10001"}}` /
  `{"type":"reply","data":{"id":"<message_id>"}}` /
  `{"type":"face","data":{"id":"178"}}`. Never flatten fields to the segment
  top level.
- This list is the set of legal structures, not slots to fill. A `reply`
  segment is optional, at most one, and must be `content[0]`; use it only when
  the quote is genuinely needed to make clear what you are answering.
- `kind:"meme"` is its own single bubble; copy the hash from `<saved-memes>`
  exactly. A meme that has since been removed from the collection fails the
  call before anything is sent.
- Empty `messages` is invalid — when there is nothing to say, do not call
  this tool at all.

## When to call

The normal speaking flow is: store your analysis with `reply` and wait; when
`<reply-task-completed>` appears you are woken with the latest timeline;
re-read it, then decide — stay silent, investigate with other tools first, or
call `send_messages` with the wording you choose **now**, informed by
everything that arrived while you waited.

The completed row is a fact about the wait being over. It is not an order to
speak and not a permission slip: the tool executes whenever called, with or
without it. Outside recovery of your own interrupted flow, do not call
`send_messages` without having gone through `reply` first — that discipline
is yours to keep, nothing will enforce it for you.

## Reading the result

The result carries a `status` plus per-bubble receipts. That receipt list,
on your own `<tool-call name="send_messages">` row, is the record of what
actually reached QQ — there is no separate row for it.

- `status:"sent"` (success) — every bubble confirmed out. The thing is said;
  never send the same content again.
- `status:"partial"` (error) — judge strictly by the per-bubble receipts:
  bubbles marked `sent` are out, the rest are not. Never resend a bubble that
  is already out. If completing the thought is still worth it, compose a
  **new** call covering only what never made it — and first ask yourself
  whether it still reads naturally in the chat.
- `status:"failed"` (error) — nothing was delivered; the `<tool-call>` error
  is the whole story. You may reorganize and send a new call if speaking is
  still right.
- `status:"uncertain"` (error) — at least one bubble **may already be out**
  (the connection broke mid-send, or the platform answered without a message
  id). Do not "re-send to be safe": a new call is a new command that really
  sends again, so if the words did land the group sees them twice. Wait —
  your call's receipts and how people react tell you what happened; only
  speak again when the timeline shows the words never arrived.

Failures like `invalid_arguments` carry a `reason_code` naming the exact
problem (bad segment shape, too many bubbles, unknown meme hash…); fix the
payload instead of retrying it unchanged.

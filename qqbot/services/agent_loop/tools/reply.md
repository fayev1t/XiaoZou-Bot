# Tool: reply

`reply` is step one of speaking: it stores your resolved analysis of the
situation and starts a wait. Each call appends **one self-contained
revision** to the current scope's short-lived draft; a successful result
means **waiting**, not sent. When the wait ends, the analysis returns to the
timeline as `<reply-task-completed>` and wakes you — that tick you re-read
the latest timeline and either stay silent, investigate first, or put the
final wording into `send_messages`; only that call's per-bubble receipts
record words/images that really reached QQ.

Ordinary speech is two arguments and nothing else:

```json
{
  "analysis": "20:30 李四在与张三讨论火锅；20:31 他在 MSG_42 单独@我问明天天气，因此真正指向我的只有天气线，火锅线仍是李四与张三之间的对话。天气工具随后确认明天有雨，气温未知。待解决内容是李四的天气问题；不要把未知气温当成已知事实。",
  "hold_seconds": 8
}
```

**Every call stands alone.** You never pass a task id, never copy a revision,
and nothing you wrote before is merged into what you write now. Hand over the
complete analysis *as of this tick*, and how long to hold it. When you call
again on a later tick, that new call is the complete replacement: repeat
anything that must remain and omit anything you are withdrawing.

There is no `upsert` — appending is what happens when you don't say otherwise.
`action` exists only for the one rare branch at the bottom of this page.

This call carries no message text. Wording that has to land exactly still
gets described in `analysis`; you will write the actual words later, in
`send_messages`, with the freshest timeline in front of you.

## `analysis` — resolve the conversation for your later self

Free text. No fixed shape, no field list, no length target — one line when one
line is all there is, several sentences when the situation is tangled.

Write the resolved map of the situation, which your later tick should not
have to derive again. It will see the same raw timeline, but you must
synthesize the tangled logic now instead of pointing your future self back at
those rows. Include whichever of these dimensions actually matter:

- who is speaking to, quoting or @-ing whom; mention a social relationship only
  when the timeline or memory actually supports it
- the active topic threads, what each contribution means inside its own thread,
  and which thread contains the unresolved matter
- decisive times/order: when a thread started, what arrived later, and which new
  message or tool result changed the interpretation
- the exact question, claim or request still needing an answer, plus the logical
  conclusion the available evidence supports
- verified facts and tool results that must stay exact, uncertainty that must
  remain uncertain, and unrelated/already-settled threads to leave alone

This is a concise memo, not raw transcript duplication. It is analysis, not a
draft of the reply: conclusions like “the claim is false; the verified value
is X” belong here; the sentence you will actually type does not — by the time
you send, the chat may have moved and the wording should be chosen then.

There is no dedicated message-id field. If you need to pin down exactly which
message is being answered, name it in the analysis — `待回答的是 MSG_42`.

## `hold_seconds` — how long to hold it

**Required; there is no default.** It is the wait before the draft completes
and comes back to you, and the **newest call replaces it outright** — later or
earlier, your call.

You are choosing it from what the tail of the timeline looks like:

| The person looks like they are… | hold_seconds |
|---|---|
| mid-sentence — half a line, "我想问一下", clearly not finished | ~15 |
| pasting something long in parts, several messages back to back | ~30 |
| done — a complete question that has been sitting there | ~8 |
| not waiting on anyone — next installment of an agreed multi-part answer | 0 |

These are starting points, not rules; pick what the moment actually calls for.
Maximum is 90, and the draft's hard deadline is fixed 90s from when it was first
created — holding again never pushes that back. The deadline bounds the
**wait**, not the send: when it forces completion you are woken to decide,
nothing goes out by itself.

To work out where you stand, subtract what you can already see:
`<current now>` minus the `when=` of the `<time>` block holding that row is how long you have been holding;
`<result>.flush_at` minus `<current now>` is how long is left.

## What happens to several revisions

The scope holds one pending draft. Your calls remain visible as
`<tool-call name="reply">` history in the timeline, while the draft folds to
the newest complete analysis — that folded version is what
`<reply-task-completed>` will carry.

- **The newest row replaces every older row outright.** Changed your read of the
  situation, corrected a fact, decided a thread should not be answered after all?
  Write the complete desired analysis in a new call. Nothing omitted from
  it remains in force merely because an older row mentioned it.
- Older rows are history; nothing merges them as patches.
- To abandon the whole draft, `action="cancel"`.
- Don't re-append with nothing new to add. A tick that starts is not a reason
  to call again.

## After the wait

`<reply-task-completed>` says the wait is over — nothing more. It is not an
order to speak: silence is a normal ending, and needs no cancel or any other
call. If you do speak, one `send_messages` call holds the complete utterance
(up to 4 bubbles, at most one meme); a successful `reply` earlier does not
oblige you to. Do not keep a task open merely to wait for the completion row
— the wake is automatic.

## `action="cancel"` — drop the draft

```json
{"action": "cancel"}
```

Nothing else needed: there is at most one pending draft per scope. Pass
`reply_task_id` only to assert *which* draft you mean; if it doesn't match the
open one the call fails instead of silently cancelling a different draft.

Use it only when the wait itself should end with no completion at all — the
moment passed, someone else answered it, you misread who was being addressed.
**To change the analysis, don't cancel — just call `reply` again**; the newest
complete revision replaces the old one. And once `<reply-task-completed>` has
appeared, there is nothing left to cancel — simply decide not to send.

## Failures worth recognising

- `invalid_arguments` with `reason_code="brief_renamed_to_analysis"` /
  `"targets_gist_replaced_by_analysis"` / `"mode_removed"` / `"upsert_removed"`
  — you sent an old argument shape. Conversation topology, topics, chronology
  and content conclusions now go into `analysis`. Appending needs no action at
  all.
- `invalid_arguments` with `reason_code="verbatim_removed"` — you passed
  `messages` or `action="verbatim"`. This tool never carries final text;
  store the analysis here, then write the words in `send_messages` after
  `<reply-task-completed>`.

Never treat `<tool-call name="reply"><result>...</result>` as speech — it is a
stored wait. Success returns `reply_task_id`, `revision`, `state`,
`flush_at` and `hard_deadline`; it never returns a sent `message_id`.

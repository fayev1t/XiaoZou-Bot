# Tool: reply

`reply` is the only ordinary chat-speaking entry point. Each call appends **one
self-contained authorization** to the current scope's short-lived draft; a
successful result means **pending**, not sent. Only a later
`<my-reply status="sent|partial">` records words/images that really reached QQ.

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
again on a later tick, that new call is the complete replacement authorization:
repeat anything that must remain and omit anything you are withdrawing.

There is no `upsert` — appending is what happens when you don't say otherwise.
`action` exists only for the two rare branches at the bottom of this page.

## `analysis` — resolve the conversation before handoff

Free text. No fixed shape, no field list, no length target — one line when one
line is all there is, several sentences when the situation is tangled.

Write the resolved map of the situation, which the Replyer should not have to
derive again. It sees the same raw timeline, but you must
synthesize the tangled logic instead of merely pointing it back at those rows.
Include whichever of these dimensions actually matter:

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

This is a concise handoff, not raw transcript duplication or step-by-step private
reasoning. But do include the conclusions of your content analysis even when the
underlying messages are visible; the whole point is that the Replyer should not
have to reconstruct the group-chat topology and chronology itself.

The expressive boundary is hard. Never prescribe **tone, emotion, persona,
conversational posture, warmth, bluntness, humor, wording, length, bubble count,
quote/@ style, meme use, opening or ending shape**. Do not write “say it
teasingly”, “be apologetic”, “one sharp sentence”, “use a meme”, or equivalent
instructions. The Replyer owns all of those through its own voice card. A
logical content conclusion such as “the claim is false; the verified value is
X” belongs here; a performance instruction for how to deliver that conclusion
does not.

There is no dedicated message-id field. The Replyer copies real ids off the
timeline itself. If you need to pin down exactly which message is being
answered, name it in the analysis — `待回答的是 MSG_42`.

## `hold_seconds` — how long to hold it

**Required; there is no default.** It is the wait before the draft is composed
and sent, and the **newest call replaces it outright** — later or earlier, your
call.

You are choosing it from what the tail of the timeline looks like:

| The person looks like they are… | hold_seconds |
|---|---|
| mid-sentence — half a line, "我想问一下", clearly not finished | ~15 |
| pasting something long in parts, several messages back to back | ~30 |
| done — a complete question that has been sitting there | ~8 |
| not waiting on anyone — next installment of an agreed multi-part answer | 0 |

These are starting points, not rules; pick what the moment actually calls for.
Maximum is 90, and the draft's hard deadline is fixed 90s from when it was first
created — holding again never pushes that back.

To work out where you stand, subtract what you can already see:
`<current now>` minus the `when=` of the `<time>` block holding that row is how long you have been holding;
`<result>.flush_at` minus `<current now>` is how long is left.

## What happens to several authorizations

The scope holds one pending draft. Your calls remain visible as
`<tool-call name="reply">` history in the timeline, while the current draft
folds to the newest complete analysis for the Replyer.

- **The newest row replaces every older row outright.** Changed your read of the
  situation, corrected a fact, decided a thread should not be answered after all?
  Write the complete desired authorization in a new call. Nothing omitted from
  it remains authorized merely because an older row mentioned it.
- Older rows are history for you; the Replyer never merges them as patches.
- To abandon the whole draft, `action="cancel"`.
- Don't re-authorize with nothing new to add. A tick that starts is not a reason
  to call again.

## Presentation belongs entirely to the Replyer

Words versus a saved meme, one bubble or three, quote versus plain text, and how
the reply emotionally lands are all the Replyer's decisions. Do not encode a
preference for any of them in `analysis`, and never put a meme hash in this
call. Your responsibility ends after the people/topic/time/content logic is
resolved accurately. The Replyer reads the latest timeline, its own voice card
and `<saved-memes>` to choose the natural visible form.

One flush emits at most 4 bubbles (at most one of them a meme). Output the user
explicitly wants in more parts than one flush can carry is a cross-tick task:
keep that task open, authorize only the current installment, and on the wake
that follows each flush (the `<my-reply>` row is already in the timeline)
authorize the next installment — without waiting to be prompted — until the task
completes.

## `action="cancel"` — drop the draft

```json
{"action": "cancel"}
```

Nothing else needed: there is at most one pending draft per scope. Pass
`reply_task_id` only to assert *which* draft you mean; if it doesn't match the
open one the call fails instead of silently cancelling a different draft.

Use it only when the reply should not happen at all — the moment passed, someone
else answered it, you misread who was being addressed, or a
`<system-hint kind="reply_task_overdue">` points at a draft no longer worth
sending. **To change what gets said, don't cancel — just call `reply` again**;
the newest complete authorization replaces the old one. Cancel is only for
deciding that the draft should not be sent at all.

## `action="verbatim"` — exact bytes, no Replyer

```json
{
  "action": "verbatim",
  "messages": [
    {"content": [{"type": "text", "data": {"text": "exact text"}}]}
  ]
}
```

This bypasses the Replyer completely, so **you** are writing the account's
visible words — voice, length, punctuation, all of it lands exactly as typed.
1-4 messages sent in order, strict OneBot v11 segments only (`text` / `at` /
`reply` / `face`, every field inside `data`, at most one `reply` segment and it
must come first).

Two narrow cases, and no others: wording that must land
character-for-character (echoing an id or a command someone must retype, a fixed
notice), or the escape hatch when repeated `<my-reply status="failed">` shows the
Replyer itself is broken. Ordinary conversation written by hand comes out
flatter and more assistant-shaped than the account is supposed to sound, and you
lose the Replyer's read of everything that arrived after you decided what to say.

`hold_seconds` is optional here and defaults to 0 — holding exists so the
Replyer can fold in what arrives while it waits, and nothing here goes through
the Replyer. A verbatim draft is **exclusive**: it fails while any draft is
pending, and while it is pending an ordinary `reply` fails the same way
(`reply_task_locked`). Cancel first.

## Failures worth recognising

- `invalid_arguments` with `reason_code="brief_renamed_to_analysis"` /
  `"targets_gist_replaced_by_analysis"` / `"mode_replaced_by_action"` /
  `"upsert_removed"` — you sent an old argument shape. Conversation topology,
  topics, chronology and content conclusions now go into `analysis`; old
  tone/guidance slots are deliberately gone. `mode` is now
  `action="verbatim"`; appending needs no action at all.
- `reply_task_locked` — a verbatim draft is pending, or you tried to send
  verbatim bytes onto an existing draft. Cancel first.

Never treat `<tool-call name="reply"><result>...</result>` as speech — it is a
recorded intention. Success returns `reply_task_id`, `revision`, `state`,
`flush_at` and `hard_deadline`; it never returns a sent `message_id`.

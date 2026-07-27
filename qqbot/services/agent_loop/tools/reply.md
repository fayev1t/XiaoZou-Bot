# Tool: reply

`reply` is the only ordinary chat-speaking entry point. Each call appends **one
self-contained authorization** to the current scope's short-lived draft; a
successful result means **pending**, not sent. Only a later
`<my-reply status="sent|partial">` records words/images that really reached QQ.

Ordinary speech is two arguments and nothing else:

```json
{
  "brief": "李四在和张三约火锅的间隙 @我，单独问明天天气；火锅那条线不是问我的，别接。直接给结论并提醒带伞，别展开。查到的是明天有雨，气温没查到，别编。",
  "hold_seconds": 8
}
```

**Every call stands alone.** You never pass a task id, never copy a revision,
and nothing you wrote before is merged into what you write now. Say what you
want said *as of this tick*, and how long to hold it. When you call again on a
later tick, that new call is the complete replacement authorization: repeat
anything that must remain and omit anything you are withdrawing.

There is no `upsert` — appending is what happens when you don't say otherwise.
`action` exists only for the two rare branches at the bottom of this page.

## `brief` — the whole of what you hand over

Free text. No fixed shape, no field list, no length target — one line when one
line is all there is, several sentences when the situation is tangled.

Write the *read* of the situation, which is the one thing the Replyer cannot
produce for itself. It sees the same timeline you do, at the same fidelity, so
it does not need content relayed to it; what it lacks is your analysis. Anything
that helps it land the reply belongs here, in whatever terms fit:

- what is going on in the room, and which thread this reply enters
- who is talking to whom, and what the message actually means in that thread
- what you want said, and what you deliberately do not want said
- facts that must stay exact (especially anything a tool just told you)
- the spirit of it — blunt, teasing, careful, apologetic
- anything situational: this person hates being called by full name; this picks
  up a joke you made earlier; don't end on a question

Two things never belong in it: **final wording** (the Replyer writes the visible
words — handing it a finished line makes it a typist) and **a retelling of
messages the timeline already shows**.

No message ids either. The Replyer copies those off the timeline itself. If you
need to pin down exactly which message is being answered, just say so in the
text — `回 MSG_42 那条`.

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
folds to the newest complete brief for the Replyer.

- **The newest row replaces every older row outright.** Changed your read of the
  situation, corrected a fact, decided a thread should not be answered after all?
  Write the complete desired authorization in a new call. Nothing omitted from
  it remains authorized merely because an older row mentioned it.
- Older rows are history for you; the Replyer never merges them as patches.
- To abandon the whole draft, `action="cancel"`.
- Don't re-authorize with nothing new to add. A tick that starts is not a reason
  to call again.

## Steering the form, not picking it

Form — words versus a saved meme, one bubble or three, how blunt it lands — is
the Replyer's call, decided against the newest timeline. You still get a say:
write the intent into the brief in plain language, e.g. `"嫌弃但其实在笑，适合
配张表情包"`, `"一句话怼回去就行"`, `"这条别耍宝，他是认真在问"`. There is no
field for choosing a meme and no hash goes in this call; what you write is a
hint the Replyer weighs, not an order it must follow. Use that say often, not
sparingly: whenever the beat is reaction rather than information — 接梗、拆台、
嫌弃、装凶、起哄 — name the mood in the brief. Naming the mood is what licenses
the Replyer to answer with a picture instead of a flat line of text; a brief
that never mentions tone quietly steers everything toward plain words. That is
the intended split — steering the beat is yours, landing it is its. Whether a
fitting meme even exists is upstream of both: only what `meme_collection` has
already saved can be picked.

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

- `invalid_arguments` with `reason_code="targets_gist_replaced_by_brief"` /
  `"mode_replaced_by_action"` / `"upsert_removed"` — you sent the old argument
  shape. Everything the retired structured fields carried now goes into `brief`;
  `mode` is now `action="verbatim"`; appending needs no action at all.
- `reply_task_locked` — a verbatim draft is pending, or you tried to send
  verbatim bytes onto an existing draft. Cancel first.

Never treat `<tool-call name="reply"><result>...</result>` as speech — it is a
recorded intention. Success returns `reply_task_id`, `revision`, `state`,
`flush_at` and `hard_deadline`; it never returns a sent `message_id`.

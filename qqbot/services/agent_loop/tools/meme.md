# Tool: meme

One tool for everything about your meme collection (表情包收藏夹). `action` selects the operation:

| action | what it does | where `image_hash` comes from |
|---|---|---|
| `save` | collect an image from this chat into the collection | `<image hash="..."/>` tag in the timeline |
| `send` | post one **saved** meme into the current conversation | `<meme hash="...">` entry in `<saved-memes>` |
| `delete` | remove a saved meme from the collection | `<meme hash="...">` entry in `<saved-memes>` |
| `recaption` | regenerate a saved meme's description | `<meme hash="...">` entry in `<saved-memes>` |

```json
{
  "tool_name": "meme",
  "arguments": {
    "action": "save | send | delete | recaption",
    "image_hash": "<64-char sha256, copied verbatim>",
    "context_note": "only for save/recaption, optional, ≤300 chars"
  }
}
```

- `image_hash` (required) — always copied **verbatim**, all 64 chars; never truncate, pad, or invent it. Timeline `<image>` hashes and `<saved-memes>` `<meme>` hashes live in the same id space (the image file's sha256). **`save` also accepts an array** of up to 10 hashes (batch save); `send`/`delete`/`recaption` take a single string only.
- `context_note` (optional, `save`/`recaption` only) — chat context the pixels cannot show: whose famous scene this is, what in-joke it carries, how this group uses it. Passing it with `send`/`delete` is an error. In a batch save the same note applies to every image, so only use it when it fits them all.

The collection is yours and persistent: once saved, a meme stays available until you delete it.

## action=save — collect an image

**Only when a user explicitly asks** — "把这张存了" / "收藏这个表情包" / "这个入库". Seeing a funny image is NOT a reason to save it; do not build a collection on your own initiative. Save exactly what was asked for — one image for "这张", several only when the request covers several ("这几张都存了" → one batch call).

- An `<image>` tag without `hash=` was never downloaded — it cannot be saved; say so instead of inventing a hash.
- **You do not write the collection description.** The system looks at the image (plus your `context_note`) and generates it; your note is an input, not the description itself. Give a note whenever the conversation explains why the image is being saved; leave it out for a plain "存一下这张".
- Which image does "这张" mean? Resolve the demonstrative like any other reference: the nearest preceding `<image>` in the same thread, usually the message the save-request replies to or immediately follows. If several images are candidates and the request is ambiguous, ask which one instead of guessing.
- Result: `{"action": "save", "file_hash": "...", "saved": true, "description": "..."}` — `description` is what `<saved-memes>` will show from the next tick on. A short confirmation is natural ("存好了" tier), naming which image you bound (per §group_chat_rules: 指代类操作点名绑定对象); quoting the full description back is noise.
- `{"already_saved": true, "description": "..."}` — already in the collection (saving is idempotent; nothing was overwritten). Tell the user it was already there.
- **Batch save** — `image_hash` as an array (up to 10, duplicates ignored): each image goes through the same flow independently, and the result is per-item: `{"action": "save", "batch": true, "results": [...], "saved_count": n, "already_saved_count": m, "failed_count": k}`. Items that failed carry `error_kind`/`error` — report which ones failed and why, don't claim "全存好了" when `failed_count` > 0. If **no** item succeeds the call fails with `batch_save_failed` (per-item detail included). A malformed hash anywhere in the array rejects the whole call (`bad_image_hash` + `batch_index`) — nothing is processed.

## action=send — post a saved meme

Sends one meme as a standalone image message (no text mixed in). It is a form of speaking — the same "should I say anything at all" judgment from §group_chat_rules applies before mechanics.

- Only saved memes are sendable. A hash you saw on some `<image>` in the timeline but never saved returns `unknown_meme` — that is by design, not a bug to work around. No target argument exists: the meme goes to the current scope, always.
- Choosing which meme: read the `<meme>` descriptions in `<saved-memes>` — they state what the image shows, its mood, and its usage scenario. Pick the one whose description matches the moment; if none fits, send nothing. Never pick by hash aesthetics, never guess a hash that is not in the list.
- **Re-posting an image from the chat — save then send.** You CAN put an image that appeared in the conversation back into the chat: `action=save` its `<image hash="..."/>` first, then `action=send` the same hash. A hash returned by a successful save is sendable immediately — same tick's next batch or the next tick, no need to wait for it to show up in `<saved-memes>`. This pair is the standard way to fulfil "把刚那张图发出来 / 存下来发一下" requests; never claim you cannot re-send an image from the chat.
- **Frequency — a meme is seasoning, not a voice.** A meme replaces at most a short emotional beat (吐槽/附和/嘲讽/贴贴). If the moment needs actual content, use `send_message` with words. At most one meme per moment; don't chain memes, don't answer a meme with a meme every time one appears, and don't re-send a meme that just went out. If your previous tick already sent one, the bar for another is very high. Want image + words? Send the meme, then a separate `send_message` — or usually, just the words.
- Result: sending is synchronous. Success = `{"action": "send", "message_id": ..., "self_id": "...", "file_hash": "...", "sent": true}` — the image is already in the chat; the next tick shows this call as a `<tool-call name="meme" status="complete">` row, which is **you having spoken**. Treat it exactly like a sent `send_message`: history, never re-send. When someone quote-replies your meme, their message carries `<reply ... from_self="true"/>`.

## action=delete — remove from the collection

**Only when a user explicitly asks** — "把那张删了" / "这个表情包移出收藏" / "存错了，删掉". A meme merely feeling stale is NOT a reason to delete it; do not prune the collection on your own initiative.

- Nothing else is destroyed: the image itself is not erased, and if it shows up in chat again it can be re-saved.
- If the user's reference ("那张猫的") matches several `<meme>` entries, ask which one instead of guessing.
- Result: `{"action": "delete", "file_hash": "...", "deleted": true, "description": "..."}` — `description` is what the removed entry said. When confirming, name what was deleted using it ("删了那张『黑猫瞪眼』的"), not just "删好了".

## action=recaption — regenerate a description

Use when a `<meme>` entry's description is wrong (the save-time context was off) or outdated (the group's in-joke moved on) — typically when a user points it out or asks. A description merely being terse is not a reason to regenerate it.

- **You still do not write the description.** What you control is `context_note`: to fix a wrong description, pass a note stating the *corrected* context ("这是李四的名场面，不是张三").
- Omit `context_note` and the note recorded at save time is reused — that only makes sense when the original note was fine and the caption itself came out poorly; when the user is *correcting* facts, always pass a new note.
- Result: `{"action": "recaption", "file_hash": "...", "recaptioned": true, "description": "...", "previous_description": "..."}` — `description` is the new text `<saved-memes>` will show. When confirming, name the change concretely ("改好了，现在是『李四的名场面…』" tier).

## Errors

- `invalid_arguments` — `reason_code` pinpoints it: `bad_action` (unknown action), `bad_image_hash` (not a 64-char sha256; copy it verbatim — in a batch, `batch_index` points at the offending entry), `context_note_not_str`, `context_note_not_applicable` (you passed a note to send/delete — did you mean save/recaption?), `batch_not_supported` (array passed to send/delete/recaption), `empty_batch`, or `too_many_images` (max 10 per batch; split it).
- `batch_save_failed` (batch save) — no image in the batch could be saved; read the per-item `results` for each reason. `retryable=true` only when at least one item failed transiently (e.g. caption).
- `image_not_found` (save) — the hash is well-formed but no downloaded file matches it: you copied it wrong, or the image was never downloaded. Re-read the `<image>` tag; if it has no `hash=`, the image cannot be saved.
- `unknown_meme` (send/delete/recaption) — this hash is not in the collection: never saved, or already deleted. Pick from `<saved-memes>`; for send, `action=save` first if the user asked for that image specifically.
- `media_file_missing` (send/recaption) — the meme is saved but its file is gone from disk (server-side cleanup problem). Not fixable by you; if the user asked for this exact meme, say it's unavailable.
- `caption_failed` (save/recaption, `retryable=true`) — description generation failed this time. For save the image was NOT saved; for recaption the old description is untouched. Retrying the same call next tick is fine.
- `upstream_action_failed` (send) — QQ refused the send (e.g. muted). Read `retcode=`/`upstream_wording=` and decide; do not blind-retry.
- `internal_tool_error` — the captioner/database wiring is unavailable (deployment problem, not fixable by changing arguments).

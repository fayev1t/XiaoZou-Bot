# Tool: save_meme

`save_meme` adds one image from this chat into your meme collection (表情包收藏夹). Saved memes appear in the `<saved-memes>` section of every later tick and can be sent with `send_meme`. The collection is yours and persistent: once an image is saved it stays available from then on.

**When to save: only when a user explicitly asks** — "把这张存了" / "收藏这个表情包" / "这个入库". Seeing a funny image is NOT a reason to save it; do not build a collection on your own initiative. One image per explicit request.

## Arguments

```json
{
  "tool_name": "save_meme",
  "arguments": {
    "image_hash": "<64-char sha256 from an <image hash=\"...\"/> tag>",
    "context_note": "张三的名场面，群里拿来嘲讽划水"
  }
}
```

- `image_hash` (required) — the `hash=` value of the image to save, copied **verbatim** from an `<image hash="..."/>` tag in the timeline. An `<image>` tag without `hash=` was never downloaded — it cannot be saved; say so instead of inventing a hash.
- `context_note` (optional, ≤300 chars) — chat context the pixels cannot show: whose famous scene this is, what in-joke it carries, how this group uses it. Give it whenever the conversation explains why the image is being saved; leave it out for a plain "存一下这张".

**You do not write the collection description.** The system looks at the image (plus your `context_note`) and generates it; your note is an input, not the description itself.

## Which image does "这张" mean?

Resolve the demonstrative like any other reference: the nearest preceding `<image>` in the same thread, usually the message the save-request replies to or immediately follows. If several images are candidates and the request is ambiguous, ask which one instead of guessing.

## Result

- Success: `{"file_hash": "...", "saved": true, "description": "..."}` — `description` is the generated text; it is what `<saved-memes>` will show from the next tick on. A short confirmation to the user is natural ("存好了" tier); quoting the full description back is noise.
- `{"already_saved": true, "description": "..."}` — this image is already in the collection (saving is idempotent; nothing was overwritten). Tell the user it was already there.

## Errors

- `image_not_found` — the hash is well-formed but no downloaded file matches it: you copied it wrong, or the image was never downloaded. Re-read the `<image>` tag; if it has no `hash=`, the image cannot be saved.
- `invalid_arguments` (`reason_code=bad_image_hash`) — not a 64-char sha256 hex string. Copy the attribute value exactly; never truncate, pad, or invent it.
- `caption_failed` (`retryable=true`) — description generation failed this time; the image was NOT saved. Retrying the same call next tick is fine.
- `internal_tool_error` — the captioner/database wiring is unavailable (deployment problem, not fixable by changing arguments).

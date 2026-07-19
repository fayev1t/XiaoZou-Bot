# Tool: meme

`meme` manages the shared saved-meme collection. It does not send anything.
When a `reply_task` is flushed, Replyer sees `<saved-memes>` and decides whether
the final reply should contain one saved meme, which meme fits, and where it
belongs among the text messages.

## Arguments

- `action` (required): `save`, `delete`, or `recaption`.
- `image_hash` (required): copy the complete 64-character SHA-256 hash verbatim.
  For `save`, use an `<image hash="..."/>` from the timeline; for `delete` or
  `recaption`, use a `<meme hash="...">` from `<saved-memes>`.
- `context_note` (optional, `save`/`recaption` only): short chat context that is
  not visible in the pixels. It guides the caption model; it is not the saved
  description itself.

`save` also accepts an array of at most 10 hashes. The other actions accept one
hash only.

## Actions

### `save`

Collect an image already present in the timeline. The system reads the image
and writes its searchable Chinese description. Use `context_note` only when
conversation context materially changes the image's meaning.

### `delete`

Remove the saved entry from the collection. This removes metadata only; it does
not delete the underlying media cache file.

### `recaption`

Regenerate a saved meme's description from the image, optionally guided by a
new `context_note`. The old description remains if captioning fails.

## Speaking boundary

Do not look for a meme-send action and do not choose a send hash in Planner.
Express the intended conversational effect in the `reply` task's `gist`.
Replyer makes the final text/meme choice once, against the latest timeline and
saved-meme catalog, when that task flushes.

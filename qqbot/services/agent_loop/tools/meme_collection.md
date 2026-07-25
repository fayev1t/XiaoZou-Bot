# Tool: meme_collection

`meme_collection` curates the shared saved-meme collection. **It never sends
anything, and there is no send action to look for.** When a `reply_task` is
flushed, the reply composer sees `<saved-memes>` and decides on its own whether
the reply should carry one saved meme, which one fits, and where it belongs
among the text messages.

So what this tool controls is not *whether a meme gets sent* — it is **what the
composer will have to choose from**. An image that never gets saved can never be
used later; a collection that is thin, stale, or badly described is what makes
replies fall flat. Curating it is the point.

## When to save

Save whenever an image goes past that you would plausibly want to answer with
some day — a reaction face, a punchline image, a group in-joke, anything with a
usable expression on it. It costs one call, the collection holds plenty, and the
image scrolls out of the timeline within the hour and cannot be recovered
afterwards; a near-miss saved beats a perfect one lost.

Do not save: photos people posted in earnest, screenshots carrying someone's
personal information, documents, or anything that only makes sense inside the
one exchange it appeared in.

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

That description is the **only** thing the composer sees when choosing later —
the pixels are not attached to it. Hence you do not write it yourself, and hence
`context_note` earns its place whenever the image's meaning in this group is not
on its face (whose reaction face it is, what running joke it belongs to).

### `delete`

Remove the saved entry from the collection. This removes metadata only; it does
not delete the underlying media cache file.

### `recaption`

Regenerate a saved meme's description from the image, optionally guided by a
new `context_note`. The old description remains if captioning fails.

Reach for this when a saved meme keeps turning up at the wrong moment, or never
turns up at all — that is a description problem, not a taste problem, and this
is the only way to fix it.

## Speaking boundary

Do not look for a meme-send action and do not choose a send hash. You cannot
send an image from here, and picking one is not your call. If you want a reply
to land as a meme rather than as words, say so in the `reply` task's `gist.tone`
or in the target's `guidance`, in plain language — the composer reads it, weighs
it against the latest timeline and the collection, and makes the final call
once, when that task flushes.

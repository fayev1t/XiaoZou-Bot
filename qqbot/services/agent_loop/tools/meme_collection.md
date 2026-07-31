# Tool: meme_collection

`meme_collection` curates the shared saved-meme collection. **It never sends
anything, and there is no send action to look for.** Sending happens only in
`send_messages`: when you compose a reply there, `<saved-memes>` is your menu,
and one bubble may carry one saved meme by its hash.

So what this tool controls is not *whether a meme gets sent* — it is **what
there will be to choose from at that moment**. An image that never gets saved
can never be used later; a collection that is thin, stale, or badly described
is what makes replies fall flat. Curating it is the point.

## When to save

Save whenever an image goes past that you would plausibly want to answer with
some day — a reaction face, a punchline image, a group in-joke, anything with a
usable expression on it. It costs one call, the collection holds plenty, and the
image scrolls out of the timeline within the hour and cannot be recovered
afterwards; a near-miss saved beats a perfect one lost.

Do not save: photos people posted in earnest, screenshots carrying someone's
personal information, documents, or anything that only makes sense inside the
one exchange it appeared in.

You are judging from the image's `desc=` in the timeline, not from the picture
itself — that transcription is enough to tell a reaction face from a document.
When it genuinely isn't enough to decide, `look_at_image` settles it; saving is
cheap enough that hesitating usually costs more than saving would.

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

That description is the **only** thing visible when choosing from
`<saved-memes>` later — the pixels are not attached to it. Hence you do not
write it yourself, and hence `context_note` earns its place whenever the
image's meaning in this group is not on its face (whose reaction face it is,
what running joke it belongs to).

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

Do not look for a meme-send action here. This tool only curates; the moment a
meme actually goes out is a `send_messages` call, chosen against the latest
timeline. Do not steer future replies toward or away from memes through
`reply.analysis` either: that field is only for people/topic/time/content
analysis — whether a meme is the natural reply is decided at sending time,
with the collection in front of you, not committed in advance.

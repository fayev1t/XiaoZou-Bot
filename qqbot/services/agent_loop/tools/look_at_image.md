# look_at_image — when and how to use

## What you already have

Every downloaded image in the timeline arrives with `desc="..."` — an objective transcription written by a vision model the moment the image was received: what is in the frame, and every piece of text in it, copied out verbatim. **That description is written with no knowledge of the conversation**, because at the time the image arrived the surrounding messages often did not exist yet. It describes; it does not interpret.

So the split is: what the image *contains* is already in your hands. What it *means here, now, in this conversation* is your job — you have the timeline, the description writer did not.

## When to call

Only when the answer you need is something the description could not have anticipated, and the pixels would settle it. That means the information is genuinely in the image but too fine-grained, too peripheral, or too dependent on the current question for a general transcription to have captured it.

Do **NOT** call it:

- To get a description. You have one. Asking again spends a vision call to receive worse information than the `desc=` already in front of you.
- To re-read text that the `desc=` already transcribed.
- On an `<image/>` with no `hash=` — that image was never downloaded and there is nothing to look at.
- To ask the same question about the same image twice. Your earlier answer is still on its `<tool-call>` row in the timeline; read it there.
- When your real uncertainty is about intent rather than content — why someone sent an image is read from the conversation, not from the picture.

## Arguments

- `image_hash` (required): the 64-char sha256, copied verbatim from `<image hash="..."/>`.
- `question` (required): the one specific thing you need to know. The model on the other end sees the image and your question — nothing else. If understanding the question requires context, put that context into the question yourself, in your own words. A vague or general question wastes the call.

## Result interpretation

You get back an answer to exactly what you asked, and nothing else — no summary of the image, no advice. If the answer says the image does not show it, believe it and do not ask a rephrased version of the same question; that is the model telling you the pixels have no more to give.

A failure means the image is unreachable (wrong hash, never downloaded, file cleaned up) or the vision backend is temporarily unavailable. Neither is fixed by retrying immediately.

## After the call

The answer appears on your `<tool-call name="look_at_image">` timeline row on the next tick, and stays there for the rest of the window — it is now part of what you know about that image, alongside its `desc=`.

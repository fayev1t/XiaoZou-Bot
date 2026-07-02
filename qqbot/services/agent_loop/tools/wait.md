# wait — when and how to use

## What it does

Schedules a wake-up for yourself after `seconds` seconds. Use it whenever the right move is to check back later rather than act now.

## When to call

- Someone is clearly mid-thought (typing a multi-part message, pasting content piece by piece) — wait instead of answering an incomplete utterance.
- You committed to a follow-up — schedule it instead of only saying it.
- A running task deserves a later look and no tool result will wake you by itself.

## When NOT to call

- To poll a tool that already wakes you when its batch completes — that wake is automatic.
- To "stay alive": don't schedule recurring waits with nothing to do. One wait, one purpose.
- Don't stack multiple overlapping waits for the same purpose; the extra wake-ups are noise.

## Arguments

- `seconds` (required, 5–3600): delay before the wake-up.
- `note` (optional but strongly recommended): your memo to your future self — why you're waiting and what to do on wake-up. It is echoed back verbatim in the wake-up hint, so write it as an instruction you'll act on.

## What happens

1. The call returns immediately with `{"scheduled": true, "wake_at": ...}` — the confirmation tick right after may show this completed call; that alone is **not** a reason to speak or act.
2. When the timer fires, you get a new tick whose timeline ends with `<system-hint kind="wait_elapsed">{"seconds": N, "wake_at": "...", "note": "your memo"}</system-hint>`. Read your note and do what it says (or idle, if the situation resolved itself meanwhile — check the timeline first).

## Caveat — timers do not survive restarts

The timer lives in process memory. If the bot restarts before `wake_at`, the wake-up is lost. You can tell: your `wait` tool-call is in the timeline (with its `wake_at`), but no matching `wait_elapsed` hint ever appeared and `now` is already past `wake_at`. In that case, re-schedule if it still matters.

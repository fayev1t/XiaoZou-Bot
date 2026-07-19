# webfetch — when and how to use

## When to call

- Someone pastes a URL in the group and you need its actual content to respond (这是什么文章 / 这仓库是干嘛的 / 链接里说了啥).
- A `websearch` result you already have is promising but its snippet is insufficient — expand that ONE url. (If you know at search time you'll need bodies, prefer `fetch_top_n` on `websearch` instead of searching then fetching one by one.)
- You already know the exact page (official docs, an announcement, a release page) and just need to read it — no search step needed.

Do **NOT** call webfetch for:
- Finding information when you have no URL — use `websearch`.
- Pages that need a login (private repos, feeds, QQ-internal links) — you have no login state; you'd only get a shell page.
- Retrying a URL that just failed or returned near-empty text — JS-rendered single-page apps yield no body over plain HTTP. One attempt is enough; fall back to `websearch` snippets or say you couldn't read it.

## Arguments

- `url` (required): absolute http/https URL, exactly as given or as returned by websearch. Loopback / private-network addresses are rejected.
- `max_chars` (optional, default 8000, max 20000): body truncation length. Raise it only when you truly need more of the page (e.g. a long article you must quote from).

## Result interpretation

`title` + `text` are the extracted readable content (scripts/styles stripped, block structure kept as line breaks); `truncated` tells you the body was cut at `max_chars`. `final_url` is where redirects landed. On failure the error message states why (HTTP status / non-text content type / network error / page too large) — treat it as "that site won't show us", not as something to retry in a loop.

## After the call

The result appears on your `<tool-call name="webfetch">` timeline row (status="complete") on the next tick. When replying with information from the page, cite the URL so users can verify.

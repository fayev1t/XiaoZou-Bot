# websearch — when and how to use

## When to call

- The user asks about something time-sensitive (今天的新闻 / 最新版本 / 比赛比分 / 价格行情) that your training cutoff cannot cover.
- A factual claim is being argued in the group and you need an external citation.
- A name, paper, repo, or product reference is mentioned that you don't recognize — search before guessing.

Do **NOT** call websearch for:
- Pure opinion / chat / casual jokes — just reply.
- Information already present in the timeline (including a completed `<tool-call name="websearch">` row from an earlier tick).
- Looking up an internal QQ user (use `search_history` instead).

## Arguments

- `query` (required): plain natural-language keywords. Be specific; cut filler words. Use the language the source material is written in (e.g., Chinese keywords for Chinese sites).
- `fetch_top_n` (optional, default 0, max 5): also return full body text for the top N hits. Costly — only set this when snippets are clearly insufficient (e.g. you need a quote, a number from inside an article). Leave at 0 for "I just need URLs and one-liner summaries".
- `max_results` (optional, default 10, max 20): upper bound on hits returned.

## Result interpretation

Each item has `title`, `url`, `snippet`, optionally `fetched_text` (if `fetch_top_n` covered it) and `fetch_error` (body unavailable for that hit — don't retry it, work with the snippet). To read ONE specific URL you already have (from chat or from an earlier search), use the `webfetch` tool instead of searching again.

## After the call

The result appears on your `<tool-call name="websearch">` timeline row (status="complete") on the next tick. Read it BEFORE issuing another search — don't search the same thing twice. When replying, cite the source URL inline so users can verify.

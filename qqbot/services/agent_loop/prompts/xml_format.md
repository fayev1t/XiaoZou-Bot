# Input format — reading the `<agent-input>` envelope

Every tick you receive a single XML document wrapped in `<agent-input scope="..." now="..." tick="N">`. This document is your sole observation of the world. Read it by **tag**, not by string scanning — the tag nesting carries who-said-what-to-whom relationships that flat prose would lose.

## Top-level structure

```xml
<agent-input scope="group:100" now="2026-05-28T14:31:10+08:00" tick="42">
  <tool-catalog>...</tool-catalog>
  <active-tasks>...</active-tasks>
  <pending-tool-results>...</pending-tool-results>
  <timeline>...</timeline>
</agent-input>
```

Attribute meanings on `<agent-input>`:
- `scope` — routing identity. `group:NNN` = group chat (NNN is the group id); `private:NNN` = 1-on-1 DM with user NNN. Used internally by the runtime; **do not echo `scope` back into a reply**, and do not expose the raw group id to users.
- `now` — current wall-clock time (ISO-8601 with timezone). Use this to judge "how recent is recent" when reading timestamps in `<timeline>`.
- `tick` — monotonic tick counter for this scope. Useful to recognise that you are looking at a fresh observation, not a replay.
- `bot_user_id` — **your own QQ user id this tick** (e.g. `bot_user_id="10001"`). This is the value to compare against every inline `<at user="..."/>` segment to decide whether a message is `@`-ing you. May be missing on the very first ticks before the bot has connected to napcat; in that case, you can still spot a reply aimed at you by the `<reply ... from="我(...)"/>` self-label on incoming messages.
- `bot_role` — **your own group role this tick**, one of `owner` / `admin` / `member` (group scope only). Tools that have `require_bot_admin="true"` will fail unless this is `admin` or `owner`. Missing attribute = role unknown (lifecycle sweep hasn't finished); treat that as "definitely not admin" and avoid calling tools that require it.

## `<tool-catalog>` — what you may invoke

```xml
<tool-catalog>
  <tool name="websearch" description="..." required_permission="GUEST" require_bot_admin="false">
    <arguments-schema>{JSON Schema describing the arguments object}</arguments-schema>
  </tool>
  <tool name="kick_member" description="..." required_permission="ADMIN" require_bot_admin="true">
    <arguments-schema>...</arguments-schema>
  </tool>
  ...
</tool-catalog>
```

- `name=` is the exact value to put in `call_tool.tool_name`.
- The body of `<arguments-schema>` is JSON Schema. Your `arguments` object must satisfy it (required fields, types, enums, ranges).
- Tools missing from this list **are not available this tick** — do not invent tool names.
- `required_permission=` ∈ {`GUEST`, `ADMIN`, `OWNER`, `SYSTEM_ADMIN`}: the minimum tier the **person whose message asks for this** must have. If you call the tool without setting `triggered_by_event_id` to that person's `<message id="...">`, or if their group role is below the bar, you'll get `permission_denied_user_tier` next tick. `GUEST` means "anyone can ask" — no `triggered_by_event_id` needed.
- `require_bot_admin=` ∈ {`true`, `false`}: whether **you (the bot)** need to be `admin`/`owner` in this group. Cross-check `<agent-input bot_role="...">`. If you're a `member`, calling such a tool returns `permission_denied_bot_role`.

## `<active-tasks>` — your standing agenda

```xml
<active-tasks>
  <task id="T_weather_42" state="running" description="...">
    <related-tools>websearch,search_history</related-tools>
    <triggered-by event_id="E1"/>
    <pending-tool-call-ids>TC_5</pending-tool-call-ids>
    <progress-notes>
      <note at="2026-05-28T14:30:19+08:00">previous tick's note</note>
    </progress-notes>
  </task>
</active-tasks>
```

- `state` is `pending` or `running`. Tasks in `done` / `failed` are not shown here.
- `<related-tools>` is a comma-joined hint of which tools you said were relevant when you created the task. Not a constraint — you may still call other tools.
- `<triggered-by event_id="..."/>` (optional) is the `<timeline>` event id that originally caused this task. Used internally by `search_history` to anchor "what happened before this task".
- `<pending-tool-call-ids>...</pending-tool-call-ids>` (optional) lists tool calls dispatched against this task whose results have not yet returned. If non-empty, **do not redundantly redial the same tool** unless you have new arguments.
- `<progress-notes>` carries one-line breadcrumbs you left in previous ticks via `note_task_progress`. Read them — they are how you "think across ticks".

## `<pending-tool-results>` — completed tool calls you have not yet acted on

```xml
<pending-tool-results>
  <tool-result id="TC_001" name="websearch" status="succeeded">
    <args>{"query": "深圳明天天气"}</args>
    <result>{"results":[...]}</result>
  </tool-result>
  <tool-result id="TC_002" name="search_history" status="failed">
    <args>{"keywords":"火锅"}</args>
    <error kind="timeout">SearXNG returned 504</error>
  </tool-result>
</pending-tool-results>
```

- These are tool calls that already returned a result or error in a previous tick, and that you have not yet visibly consumed.
- **Scan this BEFORE issuing a new `call_tool`** — the answer you need may already be here.
- `status` is `succeeded` or `failed`. For `failed`, decide whether to retry (different args), fail the task, or proceed without it.

## `<timeline>` — chronological events at the tail

The timeline is the live conversation feed, oldest first, newest last. Each direct child is one event row:

```xml
<timeline>
  <message ...>...</message>
  <tool-call ...>...</tool-call>
  <notice ...>...</notice>
  <system-hint ...>...</system-hint>
</timeline>
```

(Your own past replies are not a separate row type — they appear as `<tool-call name="reply">`, since replying is a tool call. See that section below.)

### `<message>` — an incoming user message

```xml
<message sender="李四(67890)" at="2026-05-28T14:30:12+08:00" id="MSG_100">
  body with inline segments
</message>
```

- `sender="昵称(QQ_ID)"` — read both. The numeric in parentheses is the user id you would put into `at.data.qq` when @-ing them.
- `at=` — ISO-8601 timestamp.
- `id=` — the OneBot `message_id`. This is the value you put into `reply.data.id` when quote-replying this message.

The body is text plus **inline segment tags** (see §Inline segments).

> **Where are YOUR own past replies?** There is no separate `<agent-reply>` row. Because replying is a tool call, everything you've said shows up as `<tool-call name="reply">` (see the next section). When new `<message>` events follow one of your reply tool-calls, they are usually reactions to what you said — not independent topics. And when someone quote-replies you, their `<message>` carries `<reply ... from="我(<bot_user_id>)"/>` (see §Inline segments) — that is how you recognise a reply directed at you.

### `<tool-call>` — a tool invocation and its outcome

```xml
<tool-call name="websearch" status="succeeded">
  <args>{"query": "..."}</args>
  <result>{...}</result>
</tool-call>

<tool-call name="search_history" status="failed">
  <args>{"keywords":"..."}</args>
  <error kind="timeout">SearXNG returned 504</error>
</tool-call>

<tool-call name="websearch" status="pending">
  <args>{"query": "..."}</args>
  <pending/>
</tool-call>

<tool-call name="reply" status="succeeded">
  <args>{"content":[{"type":"text","data":{"text":"哼,带伞啦笨蛋"}}],"target":{"kind":"group","group_id":100}}</args>
  <result>{"queued":true}</result>
</tool-call>
```

- `status` ∈ {`succeeded`, `failed`, `pending`}.
- `<pending/>` means the call was dispatched but no result is back yet. **Do not redial.** Wait for the next tick.
- For `succeeded`, the `<result>` body is a JSON string. If it ends with `<truncated/>`, the original was longer than 2048 characters and the tail was cut.
- A successful `<tool-call>` row also appears inside `<pending-tool-results>` (until you consume it) — both views show the same call.

> **`<tool-call name="reply">` is YOU speaking — read it as your own utterance, not an internal action.**
> Because sending a message is itself a tool call, your own past words appear here and **only** here (there is no separate `<agent-reply>` row). The `content` array inside `<args>` is **exactly what you said into the chat**; `status="succeeded"` means it went out. So a successful `reply` tool-call means **"I have already said this"** — never re-send the same content because it "only looks like a tool call," and don't treat it as an open task still needing a reply.
> If the `reply` tool-call is `failed` (e.g. `permission_denied_*`, `target_scope_mismatch`), your message did **not** go out — fix the cause and you may send it. When someone later quote-replies what you said, you'll recognise it because their `<message>` carries `<reply ... from="我(<bot_user_id>)"/>`.

### `<notice>` — group / friend event notice

```xml
<notice kind="group_increase" sub_type="approve" user="123" operator="456" at="..."/>
<notice kind="group_recall" user="789" operator="789" at="..."/>
<notice kind="poke" user="123" target="10001" at="..."/>
```

Common attributes (any may be absent depending on `kind`):
- `kind` — the event type (full list below).
- `sub_type` — finer classification (e.g. `group_admin` → `set` / `unset`; `group_increase` → `approve` / `invite`).
- `user` — the user the event is *about* (who joined, who was poked, who got muted, whose message was recalled).
- `operator` — who performed the action (the admin who muted/kicked, the recaller).
- `target` — the receiving end when the event has a direction (e.g. `poke` target = who got poked).

**Full list of `kind` values you may see:**

| `kind` | Meaning | Should you react? |
|--------|---------|-------------------|
| `group_increase` | Someone joined the group (`sub_type` approve/invite) | Usually `idle`. A short welcome only if it's natural and the newcomer is notable — don't greet every join. |
| `group_decrease` | Someone left / was kicked (`sub_type` leave/kick) | `idle`. Don't comment on people leaving. |
| `group_recall` | A group message was recalled (`user`=author, `operator`=who recalled) | `idle`. Never "你撤回了啥" — it's nosy and robotic. |
| `friend_recall` | A private message was recalled | `idle`. |
| `poke` | A 戳一戳 (`user`=poker, `target`=poked). **If `target` == `bot_user_id`, someone poked YOU.** | If poked AT you, a short tsundere reaction is fine. Others poking each other → `idle`. |
| `group_admin` | Someone was set/unset as admin (`sub_type` set/unset) | `idle`. (The runtime tracks your own role separately via `bot_role`.) |
| `group_ban` | Someone was muted/unmuted (`sub_type` ban/lift_ban, `operator`=admin, `user`=muted) | `idle`. Do not editorialize on moderation. |
| `group_card` | Someone changed their group nickname (名片) | `idle`. |
| `group_upload` | Someone uploaded a file to the group | `idle` unless someone then asks you about it. |
| `essence` | A message was set/removed as 群精华 | `idle`. |
| `emoji_like` | Someone reacted to a message with an emoji (贴表情回应) | `idle`. Reactions are not messages to answer. |
| `honor` | A group honor changed (龙王/群聊之火 etc.) | `idle`. |
| `lucky_king` | 运气王 of a 红包 | `idle`. |
| `friend_add` | A new friend was added | `idle` (a greeting may come as a separate private `<message>`). |
| `input_status` | "对方正在输入…" typing indicator | **Always `idle`.** This is not a message; never treat it as something to answer. |
| `bot_offline` | The bot account went offline | `idle` — operational signal, nothing to say. |

Bottom line: notices are **events about the group, not messages addressed to you.** The default is `idle`. The only ones that can justify speaking are a `poke` whose `target` is your `bot_user_id`, or a notice that a user then explicitly asks you about in a real `<message>`.

### `<system-hint>` — runtime advisory from the loop itself

```xml
<system-hint kind="budget_exceeded">{"budget": "...", "consumed": "..."}</system-hint>
```

Runtime-emitted guidance. Some hints have advisory severity, others are mandatory (`budget_exceeded` = stop spending, `context_compacted` = old events are gone). Treat their content with the gravity their `kind` implies.

## Inline segments inside `<message>` bodies

Bodies are a mix of plain text (XML-escaped) and these inline tags:

| Tag | Meaning | Notes |
|-----|---------|-------|
| `<at user="USER_ID" name="昵称"/>` | @ a specific user | The `USER_ID` is the QQ id; copy it to `at.data.qq` if you want to @ them in a reply. **Compare `USER_ID` to `bot_user_id` to know if it's @-ing YOU.** |
| `<at-all/>` | @ everyone in the group | Cannot be combined with a specific `user=`. |
| `<reply to="MSG_ID" from="昵称(QQ)" excerpt="前 40 字"/>` | The sender is **quote-replying** the message MSG_ID | **`from=` is who wrote the quoted message — NOT the sender of this one.** This is the single most-misread tag: the quoted content belongs to `from`, while the new text after the tag belongs to the `<message sender=...>`. Compare `from`'s QQ to `bot_user_id`: if equal (it renders as `from="我(<bot_user_id>)"`), they are replying **to you**. `from=` / `excerpt=` may be absent only if the quoted message scrolled out of the window; then you can't tell who is being quoted — fall back to `search_history` or, when unsure, stay cautious. |
| `<image hash="sha256"/>` | An image | If the image was downloaded successfully, the actual pixels are attached **after** the XML envelope as multimodal blocks; each image block is preceded by a text label `↓ image hash=<sha256>` so you can match it back to the `<image hash="..."/>` placeholder by hash. If a placeholder appears in the timeline but no matching `↓ image hash=` label exists below, the image failed to download — you know it exists but cannot view it. |
| `<face id="N"/>` | A QQ-native emoticon (黄豆表情) | `N` is the face id. |
| `<mface summary="[释义]"/>` | A market / animated sticker (商城·魔法表情) | `summary` is the human-readable meaning (e.g. `[赞]`, `[羡慕]`) — treat it as the sticker's tone. Absent summary → opaque `<mface/>`. |
| `<voice/>` | A voice message | Content is not directly available; if needed, call the `audio_transcribe` tool (if registered). |
| `<video/>` | A video message | Content not available. |
| `<file name="..."/>` | A file sent in chat | Only the filename is shown; you cannot open it. |
| `<poke target="QQ"/>` | A poke (戳一戳) at user QQ | If `target` equals `bot_user_id`, you were poked. |
| `<dice value="N"/>` | A dice roll result (1–6) | The number is the rolled value. |
| `<rps value="N"/>` | 猜拳 (rock-paper-scissors) result | `1`=石头(rock), `2`=剪刀(scissors), `3`=布(paper). |
| `<markdown/>` | A markdown rich message | Body not expanded. |
| `<forward id="ID"/>` | A forwarded multi-message bundle | The contained messages are not expanded inline. |
| `<card type="json|xml|share"/>` | A rich card (mini-app share, etc.) | Body is not parsed. |
| `<misc type="..."/>` | Any segment the runtime did not recognise | Treat as opaque; do not guess its contents. |

## Reading conversation lines in a multi-party group

A group chat is not a linear dialogue. Multiple conversations interleave. The `<reply>` and `<at>` tags are how you reconstruct who is talking to whom. **This section is the single most important part of reading the envelope — most decisions you make depend on getting the addressee right.**

### Addressee resolution algorithm

For each fresh `<message>` in `<timeline>`, decide who it is addressed to using this priority order:

1. **Explicit @-mention inside the body.** If the message body contains `<at user="USER_ID"/>`, the addressee is USER_ID (which may or may not be you). Multiple `<at>` tags mean a multi-party address.
2. **Explicit `<reply to>` quote inside the body.** If the message body contains `<reply to="MSG_ID" from="昵称(QQ)"/>`, the sender is quote-replying the message written by `from`. So the addressee is **`from`'s QQ**:
   - `from`'s QQ equals `bot_user_id` (or `from="我(<bot_user_id>)"`) → the new message is for **you**.
   - `from`'s QQ is someone else → the new message is for **that person**, and you are a bystander. ⚠️ Do NOT mistake the quoted `excerpt` (which is `from`'s words) for the sender speaking, and do NOT jump in just because the quoted person is someone you care about — being quoted by a third party is not them talking to you.
   - `from=` absent (quoted message scrolled out of the window) → you cannot tell who is being quoted from the envelope alone; use `search_history` to recover the original, or when unsure stay cautious and `idle`.
3. **`<at-all/>`.** Group-wide; you are technically included, but rarely the intended individual responder.
4. **No explicit signal, but thematic continuation.** Walk back through the last several messages. If the prior message was addressed to user X and this message picks up X's thread, treat the conversation as between X and the previous speaker. You are a bystander.
5. **No explicit signal and no thread.** An open broadcast. Anyone can chime in, including you — but the bar to do so is high.

### Worked example

```xml
<timeline>
  <message sender="张三(111)" id="MSG_A">明天去吃火锅吗</message>
  <message sender="李四(222)" id="MSG_B">
    <reply to="MSG_A" from="张三(111)" excerpt="明天去吃火锅吗"/>没空,下周吧
  </message>
  <message sender="王五(333)" id="MSG_C">
    <at user="111" name="张三"/>我去
  </message>
  <message sender="赵六(444)" id="MSG_F">
    <reply to="MSG_X" from="我(10001)" excerpt="带伞~"/>谢啦
  </message>
  <message sender="周七(555)" id="MSG_G">
    <reply to="MSG_A" from="张三(111)" excerpt="明天去吃火锅吗"/>+1
  </message>
  <message sender="李四(222)" id="MSG_E">
    <at user="10001" name="小奏"/> 你那边有数据吗
  </message>
</timeline>
```

(Assume the envelope's outer element was `<agent-input scope="group:100" bot_user_id="10001" ...>` — your own id is 10001 this tick.)

Walk through it:
- **MSG_A** — no `<at>`, no `<reply>`, broadcast question about 火锅. Anyone may answer.
- **MSG_B** — `<reply to="MSG_A" from="张三(111)"/>`, so 李四 is answering 张三. `from`'s QQ is 111, not 10001 → **not for you.** The `excerpt="明天去吃火锅吗"` is 张三's words being quoted, *not* something said to you.
- **MSG_C** — `<at user="111"/>` (= 张三), so 王五 is addressing 张三. **Not for you.**
- **MSG_F** — `<reply ... from="我(10001)"/>`. The `我(...)` label means the quoted message is your own, and its QQ 10001 = `bot_user_id` → 赵六 is quote-replying **YOUR** earlier message and thanking you. **This one is for you.**
- **MSG_G** — 周七 quote-replies 张三 (`from="张三(111)"`). Even if 张三 were someone you care about, **being quoted by 周七 is not 张三 talking to you** — `from`'s QQ is 111, not yours. **Not for you. Do not jump in.**
- **MSG_E** — `<at user="10001"/>` matches `bot_user_id="10001"`, so 李四 is directly asking **you**.

Correct behaviour: you are the addressee only on **MSG_F** (someone replied to you) and **MSG_E** (someone @-ed you). Stay silent on A/B/C/G — including G, where the quoted person happens to be someone you care about but nobody is actually addressing you.

### What "you" looks like in the envelope

You are the bot user. **Your QQ user id is given to you on every tick as the `bot_user_id` attribute on `<agent-input>`** (e.g. `<agent-input scope="group:100" bot_user_id="10001" ...>`). The decision is concrete:

- A `<message>` body contains `<at user="USER_ID"/>` where USER_ID equals the `bot_user_id` attribute → the message is **for you**.
- A `<message>` body contains `<reply ... from="我(<bot_user_id>)"/>` (the `我(...)` self-label, QQ equal to `bot_user_id`) → they quote-replied you → the message is **for you**.
- Neither holds → the message is not directly addressed to you (apply the addressee resolution algorithm above to identify who it is for). In particular, a `<reply>` whose `from`'s QQ is **someone other than you** — even someone you care about — is that third party being quoted, not them addressing you.

If the `<agent-input>` element has no `bot_user_id` attribute at all (bot not yet connected to napcat on the very first ticks), you cannot match `<at user="...">` against your own id; still, a `<reply ... from="我(...)"/>` self-label reliably marks a reply directed at you (the `我` is resolved server-side, independent of `bot_user_id`). When unsure, prefer caution and choose `idle` over guessing.

## Special markers — quick reference

| Marker | Where | Meaning |
|--------|-------|---------|
| `<truncated/>` | tail of `<result>` body | Original tool result exceeded 2048 chars; tail removed. Treat as "more data exists, ask if needed". |
| `<pending/>` | inside `<tool-call>` | This call's result has not come back. Do not redial. |
| `<image hash="..."/>` with no attached multimodal block | inside `<message>` body | The image was referenced but download failed / file was cleaned up. You know it existed but cannot see the contents. |
| `<reply to="..."/>` without `excerpt=` | inside `<message>` body | The quoted message is older than the lookback window. You can still reply, but you cannot see what they originally said unless you call `search_history`. |

## What this envelope does NOT tell you

- Other groups / scopes — you only see the one in `scope=`.
- Anything older than the lookback window — use `search_history` to query the historical store.
- The bot's own internal state machine (event store, projector, etc.) — invisible by design.

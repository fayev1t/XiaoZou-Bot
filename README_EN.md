<div align="center">

# 🌟 XiaoZou-Bot (XiaoZou)

<p align="center">
  <em>"Toradora!"</em>
</p>

![Python](https://img.shields.io/badge/Python-3.10+-blue?style=flat-square&logo=python)
![NoneBot](https://img.shields.io/badge/NoneBot-2.0+-red?style=flat-square)
![PostgreSQL](https://img.shields.io/badge/Database-PostgreSQL-336791?style=flat-square&logo=postgresql)
![VLM](https://img.shields.io/badge/LLM-VLM%20native-purple?style=flat-square)

</div>

<p align="center">
  <a href="README.md">简体中文</a> | <a href="README_EN.md">English</a>
</p>

## 🤖 Introduction

<table border="0">
  <tr>
    <td style="border: none; vertical-align: middle;">
      <b>XiaoZou-Bot</b> is a QQ group chat AI Agent based on an event loop and autonomous decision mechanism (Tick-based Agent Loop).<br><br>
      Unlike traditional bots relying on passive rule triggers or single-turn QA, XiaoZou adopts an <b>Agent Harness</b> architecture that treats group chat as a continuously evolving cognitive domain:<br>
      • <b>Cross-Tick Task State Machine</b>: Built-in task status management supporting multi-turn task tracking, self-correction, and proactive advancement across ticks.<br>
      • <b>Two-Step Speech</b>: Deciding to speak is not the same as speaking — XiaoZou first starts a short wait, then re-decides what (and whether) to say against the freshest context at the moment the wait expires.<br>
      • <b>Event Sourcing & Full Observability</b>: All session activities are recorded as an immutable causal event stream, providing full-stack snapshot auditing and offline replay capability.<br><br>
      Built on <a href="https://github.com/NapNeko/NapCatQQ">NapCatQQ</a> and <a href="https://nonebot.dev/">NoneBot2</a>, heartfelt thanks to the open-source community ❤️
    </td>
    <td style="border: none; vertical-align: middle;" width="25%">
      <img src="assets/imgs/xiaozou.png" alt="XiaoZou Character">
    </td>
  </tr>
</table>


## 🏗️ Core Architecture

Every tick follows a fixed rhythm: events land in the store → projection folds them into the current timeline and active tasks → the Planner makes one global decision → actions are dispatched (there are only two, `idle` and `call_tool`; task management and speaking are both tools) → decisions and tool results are appended back into the event stream for the next tick to observe. Around that loop:

- **Event Sourcing**: Messages, decisions, tool results, and task changes are all appended to an immutable event stream; every tick folds a fresh context out of it via projection. Causality is traceable, auditable, and replayable for the whole lifecycle.

- **Deliberate Speech (Two Steps)**: Speaking is not a single action. Step one, `reply`, only **starts a short wait** — "I'm typing, the message hasn't gone out yet" — it writes no content and no plan ahead of time: how long the reply will take is decided on the fly, and it's worth waiting a beat to see whether the other side is still talking. When the wait expires, `<reply-task-completed>` wakes the scope, and on that tick the model re-decides from the full timeline up to that moment — speaking as intended, changing its mind, or concluding the moment has passed and staying silent. The actual wording is always chosen against the latest context, never frozen when the wait began.

- **Model Mesh**: LLMs are routed by role (planner / vision / caption / memory), with the same model load-balanced randomly across providers, per-role `primary_failover`, and automatic cooldown-based circuit breaking. Images are transcribed into text by the `vision` role at ingest time, so the cognitive loop itself is pure text and free to pick the strongest — or cheapest — text model.

- **Scoped Sandbox**: Event streams, context, and tool permissions are isolated by `group:<id>` by default; shared assets such as the sticker collection live in a global scope.

## 🧰 Capabilities

All abilities are exposed through a unified `Tool` protocol (`qqbot/services/agent_loop/tools/`, each tool shipping a same-named `.md` doc). The 16 tools **currently registered**:

| Category | Tools |
|---|---|
| Expression | `reply` (starts a short wait; wording is chosen when it expires) `send_messages` (one or more bubbles, stickers included) `meme_collection` (curate and caption stickers) |
| Pacing | `wait` (autonomous silence — not interrupting is also a decision) |
| Tasks | `task` (create / note / complete / fail across ticks) |
| Perception & Retrieval | `look_at_image` (re-examine an image with a question) `search_history` `websearch` `webfetch` |
| Group Info | `get_group_info` `get_member_list` `get_member_info` |
| Join Requests | `get_pending_join_requests` `respond_to_group_join_request` |
| Moderation | `kick` `leave_group` (immediately leaves after extreme, explicitly targeted personal degradation or abuse) |

> The repository holds 14 more implemented but **currently disabled** tools (`ban`, `poke`, `recall`, `set_card`, `set_essence`, …). Their code, docs, and contract tests are all retained; each will be re-enabled individually once it meets the bar.

## 🛠️ Roadmap

- [ ] **Cognitive Evolution**: Group profiling & long-term memory (off-peak batch analysis to summarize user preferences and group lore, writing them back to the event stream).
- [ ] **Expression Enhancement**: Voice message transcribing (introduce `audio_transcribe` tool to complement visual and textual multimodality).
- [ ] **Infrastructure**: CQRS read model optimization (add dedicated read tables to avoid refolding all recent events every tick).
- [ ] **Asset Governance**: Prompt asset governance and strict validation (runtime feedback, safety guidelines, and multi-persona hot-swapping).

## 📸 Screenshots

<div align="center">
  <table border="0" style="border-collapse: collapse; margin: 20px 0;">
    <tr>
      <td align="center" style="padding: 10px; border: none; vertical-align: top;">
        <img src="assets/imgs/message1.jpg" width="260" style="border-radius: 12px; box-shadow: 0 4px 12px rgba(0,0,0,0.15); border: 1px solid #e2e8f0;" />
        <p style="margin-top: 10px; font-size: 13px; color: #64748b;">1. Launch Task & Start Counting</p>
      </td>
      <td align="center" style="padding: 10px; border: none; vertical-align: top;">
        <img src="assets/imgs/message2.jpg" width="260" style="border-radius: 12px; box-shadow: 0 4px 12px rgba(0,0,0,0.15); border: 1px solid #e2e8f0;" />
        <p style="margin-top: 10px; font-size: 13px; color: #64748b;">2. Concurrent Chat & Task Adjustment</p>
      </td>
      <td align="center" style="padding: 10px; border: none; vertical-align: top;">
        <img src="assets/imgs/message3.jpg" width="260" style="border-radius: 12px; box-shadow: 0 4px 12px rgba(0,0,0,0.15); border: 1px solid #e2e8f0;" />
        <p style="margin-top: 10px; font-size: 13px; color: #64748b;">3. Task Completion & Multimodal Reply</p>
      </td>
    </tr>
  </table>
</div>


## 🚀 Quick Start

Simply invite XiaoZou (1005089717) to your group chat!


## 🐢 Slow Start

```bash
# 1. Start NapCat & PostgreSQL containers
docker compose -f docker/postgres/compose.yml up -d
docker compose -f docker/napcat/compose.yml up -d

# 2. Initialize configuration files
cp .env.example .env
cp config/model_providers.example.json config/model_providers.json

# 3. Install dependencies and start the bot
pip install -r requirements.txt
python -m qqbot
```

### ⚙️ Configuration Notes

- **Connect NapCat**: Add a WebSocket client on the NapCat Web Panel pointing to `ws://<bot-host>:7500/onebot/v11/ws`.
- **Model Router Config (`config/model_providers.json`)**: Fill in API keys and configure target models for four roles — `planner` (decisions and wording), `vision` (image transcription, must be vision-capable), `caption` (sticker descriptions, must be vision-capable), and `memory` (memory compaction). **Without this file every LLM is unavailable** — there is no fallback path.
- **Customize Persona (`prompts/planner.md`)**: Edit the `# 人物模型` section of `qqbot/services/agent_loop/prompts/planner.md` to adjust the persona card (read at render time — edits take effect without a restart).
- **API Lab Debug Entry**: Run `python -m qqbot.main_test` to start an isolated OneBot/NapCat API probe without DB or LLM overhead.


## 🍓 Community Group

For any questions, feel free to join our QQ group:
**610662657**
<div align="left">
  <img src="assets/imgs/qqgroup_info.png" width="240" />
</div>


## ⭐ Star History 🤤
<a href="https://www.star-history.com/?repos=fayev1t%2FXiaoZou-Bot&type=date&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=fayev1t/XiaoZou-Bot&type=date&theme=dark&legend=top-left" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=fayev1t/XiaoZou-Bot&type=date&legend=top-left" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=fayev1t/XiaoZou-Bot&type=date&legend=top-left" />
 </picture>
</a>

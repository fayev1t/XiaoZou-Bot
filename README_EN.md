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
      • <b>Cognitive & Expression Layering</b>: High-level planning (LLM Planner) handles situational awareness and decisions, while an independent expression engine (Replyer) manages nuanced composition and visual grounding.<br>
      • <b>Event Sourcing & Full Observability</b>: All session activities are recorded as an immutable causal event stream, providing full-stack snapshot auditing and offline replay capability.<br><br>
      Built on <a href="https://github.com/NapNeko/NapCatQQ">NapCatQQ</a> and <a href="https://nonebot.dev/">NoneBot2</a>, heartfelt thanks to the open-source community ❤️
    </td>
    <td style="border: none; vertical-align: middle;" width="25%">
      <img src="assets/imgs/xiaozou.png" alt="XiaoZou Character">
    </td>
  </tr>
</table>


## 🏗️ Core Architecture & System Abstractions

The project is built around the **Agent Loop / Harness** philosophy, featuring five core architectural abstractions:

- **Event Sourcing & State Projection**  
  All incoming messages, model decisions, tool execution results, and task state changes are appended as immutable events into `agent_events`. Every tick folds and projects recent events into a decision context (Timeline + Active Tasks), enabling end-to-end causal traceability, auditing, and lossless replay.

- **Cognitive & Expression Layering (Planner & Replyer)**  
  - **High-Level Decision Engine (LLM-as-Planner)**: Focuses purely on situational awareness, dialogue comprehension, and tool orchestration, emitting structured Actions (task lifecycle / tool calls / autonomous silence).
  - **Multimodal Expression & Rendering (Replyer Engine)**: Upgrades "messaging" into an asynchronous composition task (`reply_task`). Persona formatting (Voice), segment layout, and visual multimodal validation (VLM) are handled by a dedicated Replyer, isolating high-level planning from low-level rendering details.

- **Model Infrastructure Mesh & Resiliency**  
  Abstracts an LLM routing layer by role (Planner / Replyer / Caption). Supports model-name based load balancing (random distribution across providers), automatic failure backoff, and passive circuit breaking, ensuring the cognitive loop remains provider-agnostic.

- **Tool Protocol & Sandboxed Isolation (Scoped Tools)**  
  Abilities like web search (`websearch`/`webfetch`) and group moderation (`kick`) are accessed via a unified `Tool` protocol. Event streams, context, and tool execution are sandboxed by group (`group:<id>`), while shared assets (e.g., sticker collections) operate in a global scope.

- **Full-Stack Observability & Replay Evaluation**  
  Every tick decision supports full-payload Prompt/XML snapshotting (`prompt_snapshot`), enabling offline comparison and regression testing (`replay_snapshots`) against fixed datasets for data-driven prompt and model optimization.

## 🛠️ Roadmap

- [ ] **Cognitive Evolution**: Group profiling & long-term memory (off-peak batch analysis to summarize user preferences and group lore, writing them back to the event stream).
- [ ] **Expression Enhancement**: Voice message transcribing (introduce `audio_transcribe` tool to complement visual and textual multimodality).
- [ ] **Infrastructure**: CQRS read model optimization (add dedicated read tables to avoid refolding all recent events every tick).
- [ ] **Asset Governance**: Prompt registry expansion (add runtime feedback, safety guidelines, and multi-persona hot-swapping).

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
- **Model Router Config (`config/model_providers.json`)**: Fill in API keys and configure target models for `planner`, `replyer`, and `caption` roles.
- **Customize Persona (`prompts/voice.md`)**: Edit `qqbot/services/agent_loop/prompts/voice.md` to adjust Replyer's persona card (reboot the process to take effect).
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

[简体中文](README.md) | [English](README_EN.md)

***

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

## 🤖 Who is she?

<table border="0">
  <tr>
    <td style="border: none; vertical-align: middle;">
      XiaoZou is a group chat assistant built on <b>VLM (Vision-Language Model) multimodal large models</b>. She is not a simple echo bot that just "receives a message → replies with a message", but an <b>event-driven / task state machine-driven</b> autonomous Agent. She decides on her own when to stay silent, who to @, whether she needs to perform a websearch before replying, and keeps track of how many unfinished tasks she currently has. She maintains her own "todo list" across multiple ticks and judges whether new incoming messages are relevant to her current tasks. 🎭<br><br>
      Beyond her fun persona, she possesses practical capabilities: <b>native image understanding</b> 📸, <b>websearch</b> 🔍, and full support for OneBot V11 segments. All capabilities are triggered naturally through <b>LLM semantic decisions</b>. ✨<br><br>
      Heartfelt thanks to <a href="https://github.com/NapNeko/NapCatQQ">NapCatQQ</a> and <a href="https://nonebot.dev/">NoneBot2</a> ❤️
    </td>
    <td style="border: none; vertical-align: middle;" width="25%">
      <img src="assets/imgs/xiaozou.png" alt="XiaoZou Character">
    </td>
  </tr>
</table>

## ✨ Key Features of v2.0 Refactor

v2.0 is a complete rewrite based on the ideas of AGENT LOOP/HARNESS. The core change is replacing the "message → reply" request-response model with **Event Streams + Task State Machine + Agent Decision Loop**:

| Feature | v1 Old Path | v2 New Path |
|---|---|---|
| **Data Engine** | Multiple business tables (users, groups, messages, tool calls) | Single `agent_events` table (Event Sourcing); other views projected on demand |
| **Trigger Model** | Immediate decision on message receipt → Immediate reply | AgentLoop periodic / event-driven tick; LLM autonomously decides idle / multiple actions within one tick |
| **Task Concept** | None; each message processed independently | Explicit `active_tasks` state machine (pending → running → done/failed), persisting across ticks |
| **Images** | Invoke "image tool" then ask secondary question | VLM native multimodality: image bytes sent directly as image_url blocks along with HumanMessage, de-duplicated by hash |
| **Reply Segments** | Text only | Full OneBot V11 segments: text / at / at-all / reply quote reply / face emoji, taught to the LLM via prompts |
| **System Prompt** | Hardcoded strings | `PromptRegistry` assembled from multiple sections: persona / protocol / reply_usage / tools_usage, with each section in an independent `.md` file |
| **Tools** | Hardcoded registration | `Tool` Protocol + sibling `.md` usage instructions; adding new tools does not touch the planner |
| **Isolation** | Voluntary separation between groups in business code | Enforced scope isolation (`group:<id>` / `private:<id>` / `system`); LLM cannot fetch data across scopes |

## 🏗️ Architecture Overview

```
                ┌──────────────────────────────────────────────────┐
napcat (QQ)  →  │ EventIngest Pipeline (qqbot/services/event_ingest)│
                │   mapper → media side effects → idempotency → DB │
                └────────────────────────┬─────────────────────────┘
                                         │ writes
                                         ▼
                           ┌──────────────────────────┐
                           │  agent_events (PG, JSONB)│   ←— Single Source of Truth
                           └──────────┬───────────────┘
                                      │ reads
                                      ▼
        ┌─────────────────────────────────────────────────────────────┐
        │ LoopSupervisor (qqbot/services/agent_loop)                  │
        │                                                             │
        │   per-scope AgentLoop  ─ tick ─►  Projector (fold + project)│
        │                                       │                     │
        │                                       ▼                     │
        │                                  DecisionContext            │
        │                                  (timeline + active_tasks   │
        │                                   + pending_tool_results)   │
        │                                       │                     │
        │                                       ▼                     │
        │   PromptRegistry → System  ─►  LLMPlanner (VLM)             │
        │                                       │                     │
        │                                       ▼                     │
        │                                  DecisionOutput              │
        │                                  (actions[])                │
        │                                       │                     │
        │            ┌──────────────────────────┼──────────────┐      │
        │            ▼                          ▼              ▼      │
        │      create_task /              call_tool          reply    │
        │      complete_task /                 │               │      │
        │      fail_task /                     ▼               ▼      │
        │      note_task_progress         ToolWorker     ReplySendWorker
        │                                      │               │      │
        │                                      └─►  agent_events  ◄──┘│
        └─────────────────────────────────────────────────────────────┘
```

## 🧠 Core Capabilities

Based on the unified event stream and **Agent Loop** decision architecture, XiaoZou is able to integrate into the group chat ecosystem as an autonomous agent, possessing the following core capabilities:

- 🔄 **Continuous Context & Task Tracking**: Using incremental state projection based on event streams, she can autonomously track, maintain, and concurrently execute multi-step, long-running tasks in complex group chat scenarios where multiple users frequently interrupt.
- 🖼️ **Native Multimodal Perception**: Supports multimodal inputs, allowing her to directly read and comprehend images shared in group chats.
- 🛠️ **Autonomous Tool Invocation & Information Retrieval**: When local knowledge is insufficient, she can autonomously decide to schedule tools like web search or history retrieval to gather real-time context, supporting deep reasoning and decision-making.
- 💬 **QQ Native Rich Text Interaction**: Deeply integrated with QQ interaction standards, allowing the LLM to autonomously control native interaction capabilities such as @ members, quoting replies, and specific face emojis.
- 🤫 **Lightweight Silence & Adaptive Wake-up**: Built-in multi-level filtering and reflection mechanisms enable her to automatically identify scenarios that require "no reply" and remain silent, avoiding disrupting group members.

## 🛠️ Roadmap (TODO)

- [ ] **State Machine-Driven Group Management** — Request processing (accepting friend requests / group invites), assistance with bans and message recalls.
- [ ] **Voice Tools** — Since VLM native support for audio is less mature than images, a standalone `audio_transcribe` tool will be used (while images stick to native, avoiding reinventing the wheel).
- [ ] **Group Profiles & Long-term Memory** — Off-peak batch processing to generate user preferences and group slang summaries, writing them back to the event stream.
- [ ] **CQRS Read Models** — Currently refolds all recent events every tick; plan to add `agent_tasks` and `agent_tool_calls` read tables for direct hot-path queries.
- [ ] **More PromptRegistry Sections** — Risk control guidelines / runtime reflection / multi-persona A/B testing.

## 🚀 Quick Start

Simply invite XiaoZou (1005089717) to your group chat!

## 🐢 Slow Start

```bash
# 1. Start NapCat & PostgreSQL containers
docker compose -f docker/postgres/compose.yml up -d
docker compose -f docker/napcat/compose.yml up -d
# 2. Copy config and run (VLM multimodal model is required)
cp .env.example .env
pip install -r requirements.txt
python -m qqbot
```
- **Connect NapCat**: Add a WebSocket client on the Web Panel pointing to `ws://<bot-host>:7500/onebot/v11/ws`.
- **Customize Persona**: Edit `qqbot/services/agent_loop/prompts/persona.md` (reboot the process to take effect after modifications).

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

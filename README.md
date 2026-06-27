[简体中文](README.md) | [English](README_EN.md)

***

<div align="center">

# 🌟 XiaoZou-Bot (小奏)

<p align="center">
  <em>「"龙与虎"」</em>
</p>

![Python](https://img.shields.io/badge/Python-3.10+-blue?style=flat-square&logo=python)
![NoneBot](https://img.shields.io/badge/NoneBot-2.0+-red?style=flat-square)
![PostgreSQL](https://img.shields.io/badge/Database-PostgreSQL-336791?style=flat-square&logo=postgresql)
![VLM](https://img.shields.io/badge/LLM-VLM%20native-purple?style=flat-square)

</div>

## 🤖 Who is she？

<table border="0">
  <tr>
    <td style="border: none; vertical-align: middle;">
      小奏是一个基于 <b>VLM 多模态大模型</b>打造的群聊助手。她不是「收到消息 → 回一条」的简单 echo bot，而是一个 <b>事件驱动 / 任务状态机驱动</b> 的自主 Agent —— 她会自己决定什么时候沉默、什么时候 @ 谁、是否需要先 websearch 再回答、当下手头有几件未完成的事；她会跨多个 tick 维持自己的"待办"，并在新消息进来时判断"这是不是和我手头的任务相关"。 🎭<br><br>
      除了人设有趣，她也具备实用能力：<b>原生图片理解</b> 📸、<b>websearch</b> 🔍、等 OneBot V11 段全支持。所有能力均通过 <b>LLM 语义化决策</b>自然触发。 ✨<br><br>
      也由衷致谢 <a href="https://github.com/NapNeko/NapCatQQ">NapCatQQ</a> 与 <a href="https://nonebot.dev/">NoneBot2</a> ❤️
    </td>
    <td style="border: none; vertical-align: middle;" width="25%">
      <img src="assets/imgs/xiaozou.png" alt="XiaoZou Character">
    </td>
  </tr>
</table>


## ✨ v2.0 重构亮点

v2.0 是一次基于AGENT LOOP/HARNESS 思想的全面重写，核心变化是把"消息 → 回复"的请求响应模型换成了**事件流 + 任务状态机 + Agent 决策循环**：

| | v1 旧路径 | v2 新路径 |
|---|---|---|
| **数据底座** | 多张业务表（用户、群、消息、工具调用各自一张） | 单一 `agent_events` 表（事件溯源）；其它视图按需投影 |
| **触发模型** | 收到消息立刻判定 → 立刻回复 | AgentLoop 周期 / 事件驱动 tick；一次 tick 内 LLM 自主决定 idle / 多动作 |
| **任务概念** | 无；每条消息独立处理 | 显式 `active_tasks` 状态机（pending → running → done/failed），跨 tick 持久存在 |
| **图片** | 调"识图工具"再二次提问 | VLM 原生多模态：图片 bytes 直接作为 image_url block 随 HumanMessage 发出，hash 去重 |
| **回复段** | 仅 text | OneBot V11 全段：text / at / at-all / reply 引用回复 / face 黄豆表情，由 prompt 教 LLM 使用 |
| **System prompt** | 硬编码字符串 | `PromptRegistry` 多 section 拼装：persona / protocol / reply_usage / tools_usage，每段独立 .md |
| **工具** | 硬编码注册 | `Tool` Protocol + sibling `.md` 用法说明，新增工具不动 planner |
| **隔离** | 群间靠业务代码自觉 | 强制 scope 隔离（`group:<id>` / `private:<id>` / `system`），LLM 不能跨 scope 拉数据 |


## 🏗️ 架构一览

```
                ┌──────────────────────────────────────────────────┐
napcat (QQ)  →  │ EventIngest 流水线（qqbot/services/event_ingest）│
                │   mapper → 媒体副作用 → idempotency → DB 落库    │
                └────────────────────────┬─────────────────────────┘
                                         │ writes
                                         ▼
                          ┌──────────────────────────┐
                          │  agent_events (PG, JSONB)│   ←—  唯一可信源
                          └──────────┬───────────────┘
                                     │ reads
                                     ▼
        ┌─────────────────────────────────────────────────────────────┐
        │ LoopSupervisor (qqbot/services/agent_loop)                  │
        │                                                             │
        │   per-scope AgentLoop  ─ tick ─►  Projector (折叠 + 投影)   │
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


## 🧠 核心能力

基于统一事件流与 **Agent Loop** 决策架构，小奏能够以自主代理（Autonomous Agent）的方式融入群聊生态，具备以下核心能力：

- 🔄 **连续上下文与任务追踪**：采用基于事件流的增量状态投影，在多用户高频插话的复杂群聊场景中，能够自主追踪、维护和并发执行多步长周期任务。
- 🖼️ **原生多模态感知**：支持多模态输入，能够直接阅读并理解群聊中的图像信息。
- 🛠️ **自主工具调用与信息检索**：当本地知识不足时，能够自主决策并调度联网搜索、历史记录检索等工具获取实时上下文，支撑深度推理与决策。
- 💬 **QQ 原生富文本交互**：深度融合 QQ 交互规范，由大模型自主控制 @成员、引用回复、特定黄豆表情等原生互动能力。
- 🤫 **轻量级静默与自适应唤醒**：内置多级过滤与反思机制，能自动识别"无需回复"的场景并保持静默，避免打乱群成员。


## 🛠️ 进化路线 (TODO)

- [ ] **状态机驱动的群管能力** — request 处理（加好友 / 加群同意）、禁言 / 撤回辅助
- [ ] **语音工具** — 模型对音频原生支持不如图片成熟，单独 `audio_transcribe` 工具（图片走原生不重复造轮子）
- [ ] **群体画像 / 长期记忆** — 闲时批处理生成用户偏好与群"黑话"摘要，写回事件流
- [ ] **CQRS 读模型** — 当前每 tick 重 fold 全量近期事件；新增 `agent_tasks` / `agent_tool_calls` 读表，hot path 直查
- [ ] **更多 PromptRegistry section** — 风控指南 / 运行期反射 / 多人格 A/B


## 🚀 快速开始

直接把小奏（1005089717）拉到群里！


## 🐢 龟速开始

```bash
# 1. 启动 NapCat & PostgreSQL 容器
docker compose -f docker/postgres/compose.yml up -d
docker compose -f docker/napcat/compose.yml up -d
# 2. 复制配置并运行（必须使用 VLM 多模态模型）
cp .env.example .env
pip install -r requirements.txt
python -m qqbot
```
- **对接 NapCat**：Web 面板添加 WebSocket 客户端指向 `ws://<bot-host>:7500/onebot/v11/ws`。
- **自定义人设**：编辑 `qqbot/services/agent_loop/prompts/persona.md`（修改后重启进程生效）。


## 🍓 交流群

任何问题，欢迎加入。
**610662657**
<div align="left">
  <img src="assets/imgs/qqgroup_info.png" width="240" />
</div>


## ⭐ 难道有一天上热榜了？🤤
[![Stargazers over time](https://starchart.cc/fayev1t/XiaoZou-Bot.svg?variant=adaptive)](https://starchart.cc/fayev1t/XiaoZou-Bot)

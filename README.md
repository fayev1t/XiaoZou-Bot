<div align="center">

# 🌟 XiaoZou-Bot (小奏)

<p align="center">
  <em>「龙与虎」</em>
</p>

![Python](https://img.shields.io/badge/Python-3.10+-blue?style=flat-square&logo=python)
![NoneBot](https://img.shields.io/badge/NoneBot-2.0+-red?style=flat-square)
![PostgreSQL](https://img.shields.io/badge/Database-PostgreSQL-336791?style=flat-square&logo=postgresql)
![VLM](https://img.shields.io/badge/LLM-VLM%20native-purple?style=flat-square)

</div>

<p align="center">
  <a href="README.md">简体中文</a> | <a href="README_EN.md">English</a>
</p>

## 🤖 项目简介

<table border="0">
  <tr>
    <td style="border: none; vertical-align: middle;">
      <b>XiaoZou-Bot</b> 是一个基于事件循环与自主决策机制（Tick-based Agent Loop）的 QQ 群聊 AI Agent。<br><br>
      不同于传统被动触发或单轮问答的聊天机器人，小奏采用 <b>Agent Harness</b> 架构，将群聊视为连续演进的认知场域：<br>
      • <b>跨 Tick 持续任务图谱</b>：内置任务状态机，支持多轮对话下的跨拍任务追踪、自我纠偏与主动推进。<br>
      • <b>认知与表达双层解耦</b>：高阶规划大脑（LLM Planner）负责态势感知与决策，独立表达层（Replyer）负责情绪化组稿与视觉对位。<br>
      • <b>事件溯源与完全可观测</b>：全局会话以不可变因果事件流落盘，具备全链路快照审计与因果离线回放能力。<br><br>
      本项目基于 <a href="https://github.com/NapNeko/NapCatQQ">NapCatQQ</a> 与 <a href="https://nonebot.dev/">NoneBot2</a> 构建，由衷感谢开源社区 ❤️
    </td>
    <td style="border: none; vertical-align: middle;" width="25%">
      <img src="assets/imgs/xiaozou.png" alt="XiaoZou Character">
    </td>
  </tr>
</table>


## 🏗️ 核心设计与架构抽象

项目围绕 **Agent Loop / Harness** 思想构建，具备以下核心架构抽象：

- **事件溯源与状态投影（Event Sourcing & State Projection）**  
  所有入站消息、模型决策、工具结果与任务变更均追加至不可变事件流（`agent_events`）。每个 Tick 将事件流折叠投影为当前决策上下文（Timeline + 活跃任务），全生命周期因果可追踪、可审计、可无损回放。

- **认知与表达职责解耦（Planner & Replyer Layering）**  
  - **高阶决策大脑（LLM-as-Planner）**：专注全局态势感知、对话理解与工具调度，输出结构化 Action（任务管理 / 工具调用 / 自主沉寂）。
  - **多模态表达与渲染（Replyer Engine）**：把“发信”升级为异步维持与组稿任务（`reply_task`）。具体的语气人设（Voice）、段落排版及视觉多模态校验（VLM）由专属 Replyer 独立完成，使规划大脑免受低阶渲染细节干扰。

- **模型基础设施网格（Model Mesh & Resiliency）**  
  基于角色（Planner / Replyer / Caption）抽象 LLM 路由层，支持按模型名跨服务商负载均衡（随机分摊）、自动故障退避与被动熔断，确保认知循环与具体模型端点脱钩。

- **能力协议与沙箱隔离（Scoped Tools & Sandboxing）**  
  网络检索（`websearch`/`webfetch`）、群管（`kick`）等能力均通过统一 `Tool` 接口接入。事件流、上下文及工具权限默认按群组（`group:<id>`）沙箱隔离；表情包等公共资产则在全局作用域共享。

- **全栈白盒与可观测性（Full-Stack Observability）**  
  每个 Tick 决策均支持完整落盘 Prompt/XML 快照（`prompt_snapshot`），可与固定数据集进行离线比对与回归评测（`replay_snapshots`），实现模型调优的数据驱动。

## 🛠️ 进化路线 (Roadmap)

- [ ] **认知演进**：群体画像与长期记忆（空闲期批处理分析群内黑话与用户偏好，写回事件流）。
- [ ] **表达增强**：语音消息转译（引入 `audio_transcribe` 工具补全音频模态认知）。
- [ ] **基础设施**：CQRS 读模型优化（增加读表以避免每 Tick 重新折叠全量近期事件）。
- [ ] **资产治理**：Prompt 注册表扩展（新增运行时反馈、风控指南与多人格热切换）。

## 📸 效果图

<div align="center">
  <table border="0" style="border-collapse: collapse; margin: 20px 0;">
    <tr>
      <td align="center" style="padding: 10px; border: none; vertical-align: top;">
        <img src="assets/imgs/message1.jpg" width="260" style="border-radius: 12px; box-shadow: 0 4px 12px rgba(0,0,0,0.15); border: 1px solid #e2e8f0;" />
        <p style="margin-top: 10px; font-size: 13px; color: #64748b;">1. 开启任务 & 发起报数</p>
      </td>
      <td align="center" style="padding: 10px; border: none; vertical-align: top;">
        <img src="assets/imgs/message2.jpg" width="260" style="border-radius: 12px; box-shadow: 0 4px 12px rgba(0,0,0,0.15); border: 1px solid #e2e8f0;" />
        <p style="margin-top: 10px; font-size: 13px; color: #64748b;">2. 多轮插话与任务动态调整</p>
      </td>
      <td align="center" style="padding: 10px; border: none; vertical-align: top;">
        <img src="assets/imgs/message3.jpg" width="260" style="border-radius: 12px; box-shadow: 0 4px 12px rgba(0,0,0,0.15); border: 1px solid #e2e8f0;" />
        <p style="margin-top: 10px; font-size: 13px; color: #64748b;">3. 任务结束与自主多模态回复</p>
      </td>
    </tr>
  </table>
</div>


## 🚀 快速开始

直接把小奏（1005089717）拉到群里！


## 🐢 慢速开始

```bash
# 1. 启动 NapCat & PostgreSQL 容器
docker compose -f docker/postgres/compose.yml up -d
docker compose -f docker/napcat/compose.yml up -d

# 2. 初始化配置文件
cp .env.example .env
cp config/model_providers.example.json config/model_providers.json

# 3. 安装依赖并启动服务
pip install -r requirements.txt
python -m qqbot
```

### ⚙️ 配置说明

- **NapCat 协议对接**：在 NapCat Web 面板中添加 WebSocket 客户端，指向 `ws://<bot-host>:7500/onebot/v11/ws`。
- **模型路由配置 (`config/model_providers.json`)**：填入 API Key，并配置 `planner`（规划）、`replyer`（组稿）与 `caption`（表情包描述）角色的目标模型。
- **人设语气自定义 (`prompts/voice.md`)**：编辑 `qqbot/services/agent_loop/prompts/voice.md` 调整 Replyer 组稿时的人设卡（重启进程生效）。
- **API Lab 独立调试**：运行 `python -m qqbot.main_test` 可启动无 DB/无 LLM 的单机协议探针，方便测试 OneBot/NapCat 连通性与底层 API。


## 🍓 交流群

任何问题，欢迎加入。
**610662657**
<div align="left">
  <img src="assets/imgs/qqgroup_info.png" width="240" />
</div>


## ⭐ 难道有一天上热榜了？🤤
<a href="https://www.star-history.com/?repos=fayev1t%2FXiaoZou-Bot&type=date&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=fayev1t/XiaoZou-Bot&type=date&theme=dark&legend=top-left" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=fayev1t/XiaoZou-Bot&type=date&legend=top-left" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=fayev1t/XiaoZou-Bot&type=date&legend=top-left" />
 </picture>
</a>

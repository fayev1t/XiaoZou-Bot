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
      • <b>两步发言的克制</b>：决定开口不等于立刻说话——先起一段短等待，到点结合那一刻的最新上下文决定说什么、还说不说。<br>
      • <b>事件溯源与完全可观测</b>：全局会话以不可变因果事件流落盘，具备全链路快照审计与因果离线回放能力。<br><br>
      本项目基于 <a href="https://github.com/NapNeko/NapCatQQ">NapCatQQ</a> 与 <a href="https://nonebot.dev/">NoneBot2</a> 构建，由衷感谢开源社区 ❤️
    </td>
    <td style="border: none; vertical-align: middle;" width="25%">
      <img src="assets/imgs/xiaozou.png" alt="XiaoZou Character">
    </td>
  </tr>
</table>

## 🏗️ 核心设计

每个 Tick 的节奏固定：事件落库 → 折叠投影出当前时间线与活跃任务 → Planner 全局决策 → 按 Action 分发（只有 `idle` 与 `call_tool` 两种，任务管理与发言都是工具），工具结果与决策再全部回写事件流，供下一拍观察。围绕这个循环：

- **事件溯源（Event Sourcing）**：消息、决策、工具结果、任务变更全部追加进不可变事件流；每个 Tick 通过投影（projection）折叠出当前上下文。全生命周期因果可追踪、可审计、可离线回放。
- **两步发言（Deliberate Speech）**：说话不是一个动作。第一步 `reply` 只**起一段短等待**——「我在输入，那条消息还没发出去」——不存内容、不做预案：还要多久才打好字自己心里有数，对方是否还在继续发言也值得先等一等再判断。等待到点写入 `<reply-task-completed>` 并唤醒当前 scope，那一拍模型从到那一刻为止的完整时间线出发，再决定是照原意开口、改口，还是判断时机已过继续沉默。落笔的字句永远是在最新上下文里选的，不是等待开始时就写死的。
- **模型网格（Model Mesh）**：按角色（planner / vision / caption / memory）路由 LLM，支持同一模型跨多服务商随机分摊负载、按角色切换 `primary_failover`、故障自动冷却熔断。群里的图片在落库期就由 vision 角色转录成文字进事件正文，认知主循环因此是纯文本的，可自由挑选最强或最便宜的文本模型。
- **群组沙箱（Scoped Sandbox）**：事件流、上下文与工具权限默认按 `group:<id>` 隔离；表情包等公共资产则在全局作用域共享。

## 🧰 能力一览

所有能力经统一 `Tool` 接口接入（`qqbot/services/agent_loop/tools/`，每个工具自带同名 `.md` 说明文档）。下表是**当前实际注册**的 16 个工具：

| 分类 | 工具 |
|---|---|
| 表达 | `reply`（起一段短等待，字句在等待结束那一刻落笔） `send_messages`（一条或多条气泡，含表情包） `meme_collection`（表情包收藏与描述） |
| 节奏控制 | `wait`（自主沉寂，不打扰也是一种决策） |
| 任务 | `task`（跨拍任务的创建 / 记录 / 完成 / 失败） |
| 感知与检索 | `look_at_image`（带问题重看图片） `search_history` `websearch` `webfetch` |
| 群信息 | `get_group_info` `get_member_list` `get_member_info` |
| 入群审批 | `get_pending_join_requests` `respond_to_group_join_request` |
| 群管操作 | `kick` `leave_group`（遭受明确指向自身的极端人格侮辱或恶意辱骂时直接退群） |

> 仓库里另有 14 个已实现但**暂未启用**的工具（`ban` `poke` `recall` `set_card` `set_essence` 等），代码、文档与契约测试都在，逐个达标后再单独放行。

## 🛠️ 进化路线 (Roadmap)

- [ ] **认知演进**：群体画像与长期记忆（空闲期批处理分析群内黑话与用户偏好，写回事件流）。
- [ ] **表达增强**：语音消息转译（引入 `audio_transcribe` 工具补全音频模态认知）。
- [ ] **基础设施**：CQRS 读模型优化（增加读表以避免每 Tick 重新折叠全量近期事件）。
- [ ] **资产治理**：Prompt 资产治理与严格校验（运行时反馈、风控指南与多人格热切换）。

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
- **模型路由配置 (`config/model_providers.json`)**：填入 API Key，并配置 `planner`（决策与措辞）、`vision`（图片转录，须带 vision 能力）、`caption`（表情包描述，须带 vision 能力）、`memory`（记忆压缩）四个角色的目标模型。**缺这个文件 = LLM 全线不可用**，没有回落。
- **人设卡自定义 (`prompts/planner.md`)**：编辑 `qqbot/services/agent_loop/prompts/planner.md` 的 `# 人物模型` 段调整人格卡（render 时才读盘，改完即生效，无需重启）。
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

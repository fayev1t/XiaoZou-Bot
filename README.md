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
      <b>XiaoZou-Bot</b> 是一个基于事件循环与决策机制（Tick-based Loop）的 QQ 群聊 AI Agent。<br><br>
      相较于其他 qqbot，基于 <b>Agent Loop</b> 架构构建的小奏天生具备如下特性：<br>
      • <b>跨 Tick 任务管理</b>：内置任务状态机，天然支持持久化任务。<br>
      • <b>真正的自主行为决策</b>：何时开口、何时沉默、要不要动用工具，都由模型自行判断，而非规则触发；@、引用、表情包、群管这些 QQ 原生能力都在她的工具箱里。<br>
      • <b>原生多模态</b>：图片和文字一样直接进入模型视野，不丢失其他细节。<br><br>
      同时，小奏也基于 <a href="https://github.com/NapNeko/NapCatQQ">NapCatQQ</a> 和 <a href="https://nonebot.dev/">NoneBot2</a> 两个项目构建，由衷感谢 ❤️
    </td>
    <td style="border: none; vertical-align: middle;" width="25%">
      <img src="assets/imgs/xiaozou.png" alt="XiaoZou Character">
    </td>
  </tr>
</table>


## ✨ 核心设计

项目基于 **Agent Loop / Harness** 思想重构，核心设计特点如下：

- **事件溯源（Event Sourcing）**：消息、决策、工具结果、任务变更都以不可变事件追加进同一条事件流，会话上下文与任务状态由这条流折叠（fold）投影得到；每个事件携带 correlation / causation 因果链，完整留痕、可回放、可追溯。
- **决策权归模型（LLM-as-Planner）**：每个 Tick 将事件流投影为决策上下文（timeline + 活跃任务）交给模型，由模型给出结构化的动作序列——开任务、调工具、推进或收尾，或者 idle。
- **能力即工具**：回复、查询、群管等能力都经统一的 `Tool` 协议接入，工具自带用法说明、按作用域控制可见性；方便能力扩展。
- **提示词模块化**：System Prompt 按职责拆成独立章节（身份 / 行为规范 / 协议格式 / 工具用法），改人设、调规则可以各自独立进行。
- **作用域隔离**：会话事件流、上下文及工具权限默认以群组（`group:<id>`）为边界进行沙箱隔离，确保各群聊决策空间互不干扰；同时支持表情包收藏等公共资产在全局作用域下跨群共享。

## 🛠️ 进化路线 (TODO)

- [ ] **补全工具体系**：重做并恢复暂时下架的联网搜索与群管工具（禁言、撤回等）。
- [ ] **语音消息转译**：引入 `audio_transcribe` 工具转译音频，弥补模型对音频原生支持的不足。
- [ ] **群体画像与长期记忆**：在空闲期批处理分析用户偏好与群内黑话，生成摘要并写回事件流。
- [ ] **CQRS 读模型优化**：改变目前每 Tick 重新折叠全量近期事件的机制，增加读表以提高直查性能。
- [ ] **丰富 Prompt 注册表**：新增风控指南、运行时反馈及多重人格热切换等 Section。

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
- **自定义人设**：编辑 `qqbot/services/agent_loop/tools/send_message.md` 的 Voice 节（角色卡只作用于发言文本；修改后重启进程生效）。


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

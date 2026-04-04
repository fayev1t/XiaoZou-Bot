<div align="center">

# 🌟 XiaoZou-Bot (小奏)

<p align="center">
  <em>「其实原型人设就是逢坂大河,但是立华奏已经在我心里留下了永远永远也无法磨灭的印记」</em>
</p>

![Python](https://img.shields.io/badge/Python-3.10+-blue?style=flat-square&logo=python)
![NoneBot](https://img.shields.io/badge/NoneBot-2.0+-red?style=flat-square)
![PostgreSQL](https://img.shields.io/badge/Database-PostgreSQL-336791?style=flat-square&logo=postgresql)

</div>

---

## 🎭 她是谁？

<!-- Mascot on the Right (Float) -->
<img src="assets/imgs/xiaozou.png" align="right" width="30%" alt="XiaoZou Character" style="margin-left: 20px; margin-bottom: 20px;">

小奏不仅是一个基于大语言模型的应答程序，她被赋予了拥有类似逢坂大河般傲娇性格的灵魂。她的核心设计理念是成为一个具有日常真实陪伴感的“赛博群友”。

**「她最大的愿望，是像一个普通的群友一样，自然地看大家聊天，在合适的时机插科打诨。」**

- 🧠 **上下文融入**：她会自动记忆近期的聊天记录，理解当前的讨论语境，拒绝缺乏逻辑的生硬回复。
- ⏳ **拟人化审时度势**：当大家讨论激烈时，她会默默潜水围观；只有在话题告一段落或适合吐槽时，才会自然地下场发表看法。
- 👁️ **跨模态视觉记忆**：发送图片给她，她不仅能精准识别图片内容，甚至可能在未来的某次闲聊中不经意地回想起这些画面。
- 👤 **群体关系感知**：她能够识别所处的群组环境、理解成员昵称与群名片，用熟人的口吻与每一个人交流。

<!-- Clear float to ensure subsequent content starts below the image area if text is short -->
<br clear="both">

## 📸 小奏的日常

😘😘😘

<div align="center">
  <img src="assets/imgs/qqgroup_message.png" width="45%" />
  <img src="assets/imgs/qqgroup_message2.png" width="45%" />
</div>

---

## 🛠️ 进化路线 (TODO)

为了让小奏在群聊中拥有更真实的陪伴感，项目计划进行以下核心功能的演进：

- [ ] **全知感知管线重构 (当前进行中)**
  重构底层的意图识别管线，使机器人在查天气、解析图片、网络搜索和日常聊天之间能够无缝切换，彻底消除传统机器人的“机械指令感”。
  
- [ ] **群聊心流与旁观机制**
  赋予机器人判断“发言时机”的能力。在群友热烈交流时适时潜水围观，避免“抢话”；并增加独立的非文本情绪机制，支持在适当的语境下单独发送表情包回应，建立属于她自己的表情图库。

- [ ] **闲时记忆提炼系统**
  摒弃死板的长期对话完整记录。系统将在无消息的闲时，自动提炼聊天记录中的高价值内容（如群友的个人偏好、群内近期发生的核心话题），并清理无意义的闲聊，建立轻量的“群体画像”。

- [ ] **群体语境自适应（黑话学习）**
  具备按群隔离的上下文理解能力，让小奏自动挖掘并学习各个群内特有的缩写、梗和专属“黑话”。使其逐渐适应不同群的聊天氛围，做到真正的入乡随俗。

---

## 🚀 快速开始

docker?(❌) requstment?(❌) 直接把小奏（1005089717）拉到群里！！！

## 🐢 龟速部署
- **基线环境**： Python 3.10，配合 NB-cli。容器化依赖（NapCat、PostgreSQL、SearXNG 与网页抓取侧车 Crawl4AI）请参考 `docker/` 目录下的 Compose 文件，其中 Crawl4AI 服务定义位于 `docker/crawl4ai/compose.yml`，应用侧通过 HTTP 调用该服务，不再依赖进程内 Python `crawl4ai`。
- **配置起点**：从项目根目录的 `.env` 入口开始，正确配置 `DATABASE_URL`、`SEARXNG_BASE_URL`、`CRAWL4AI_BASE_URL`。如果采用“应用服务器 + Docker 服务服务器”双机部署，这几个地址应指向 Docker 服务所在机器的可达地址，而不是固定写死 `127.0.0.1`；`docker/` 下各个 compose 继续保持各自独立的固定部署基线，不依赖根 `.env` 做变量插值。
- **开发与调试**：基于 NoneBot 框架，本地日常开发可通过执行 `nb run --reload` 边跑边改，实现热重载调试。
- **运行时目录**：图片缓存和其他运行时产物默认写入 `runtime_data/`，日志写入 `logs/`。

## 💬 交流社区

任何问题，欢迎加入。
**610662657**
<div align="center">
  <img src="assets/imgs/qqgroup_info.png" width="400" />
</div>

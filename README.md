一个基于 NapCat + NoneBot 的 QQ 机器人项目，目标是让机器人在群聊里更自然地理解上下文、参与对话并持续积累长期记忆。

## 项目结构

- `qqbot/plugins/`：NoneBot 插件入口，处理启动、群消息、群通知、私聊等事件
- `qqbot/services/`：业务服务层，负责消息转换、上下文、聚合、回复、图片解析等逻辑
- `qqbot/core/`：基础设施层，负责配置、数据库、调度器、日志等能力
- `qqbot/models/`：SQLAlchemy 模型与动态分表命名规则
- `docker/`：应用、PostgreSQL、NapCat 的 Docker 部署文件
- `runtime_data/`：运行时本地缓存目录，当前主要用于图片缓存
- `logs/`：运行日志目录
- `tests/`：`unittest` 契约测试
- `开发文档/`：开发规范、数据库设计、开发日志等文档

## 当前运行基线

- Python 3.10+
- 数据库后端：PostgreSQL-only
- 时间语义：统一使用 `Asia/Shanghai` 的 timezone-aware `datetime`
- `System-Message.timestamp`：渲染为 `YYYY-MM-DD HH:MM:SS` 的上海本地时间字符串
- 配置加载顺序：`.env` → `.env.<ENVIRONMENT>`

## 关键配置

根目录 `.env` 是默认配置入口。

当前最关键的运行项：

- `PORT=7500`
- `DATABASE_URL=postgresql+asyncpg://admin:mypassword@127.0.0.1:7504/mydb`
- `QQBOT_ENABLE_TEST_EVENTS=0`

说明：

- 宿主机直接运行 bot 时，默认使用根 `.env` 里的 `DATABASE_URL`
- 使用 `docker/docker-compose.yml` 启动 `qqbot` 容器时，compose 会覆盖容器内 `DATABASE_URL` 为 `postgresql+asyncpg://admin:mypassword@postgres:5432/mydb`
- 根 `.env` 里的 `NAPCAT_*`、`POSTGRES_*` 当前用于和 `docker/napcat/compose.yml`、`docker/postgres/compose.yml` 对照，不是 Python 运行时直接读取的数据库键

## Docker 文件位置

- 应用：`docker/docker-compose.yml`
- PostgreSQL：`docker/postgres/compose.yml`
- NapCat：`docker/napcat/compose.yml`

当前 checked-in 默认值：

- `qqbot` 容器用户：`1001:1001`
- `postgres` 容器用户：`1001:1001`
- `napcat` 容器用户：跟随 `${UID}` / `${GID}`
- `qqbot` 对外端口：`7500`
- `postgres` 对外端口：`7504`
- `napcat` 对外端口：`7501 / 7502 / 7503`

`docker/docker-compose.yml` 与 `docker/postgres/compose.yml` 通过命名网络 `qqbot-postgres-network` 互通，应用容器通过 PostgreSQL 服务名 `postgres` 建库连接。

## 启动方式

### 宿主机直接运行

1. 准备根目录 `.env`
2. 启动 PostgreSQL：`docker compose -f docker/postgres/compose.yml up -d`
3. 安装依赖：`pip install -e ".[dev]"`
4. 启动 bot：`nb run --reload`

### Docker 运行应用容器

1. 先启动 PostgreSQL：`docker compose -f docker/postgres/compose.yml up -d`
2. 再启动应用：`docker compose -f docker/docker-compose.yml up -d --build`

## 测试与校验

当前仓库已经有 `unittest` 契约测试。

常用检查命令：

- `python -m unittest discover -s tests`
- `ruff check .`
- `pyright`

如果只想验证数据库/配置契约，可运行：

- `python -m unittest tests.test_env_centralization_contract tests.test_postgres_only_contract tests.test_time_contract`

## 数据与缓存

- PostgreSQL 表结构由应用启动时通过 SQLAlchemy 建立
- `docker/postgres/init/` 当前为空
- 图片缓存保存在 `runtime_data/images`

## 说明

- 这个仓库目录是通过 SFTP 挂载的服务器目录；当前终端不等于真实运行环境
- 因此本地修改后的验证以静态检查和契约测试为主，完整联调需要在真实运行环境完成

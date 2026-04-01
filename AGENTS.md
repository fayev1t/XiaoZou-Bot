# Repository Guidelines
说中文

①这个目录是通过 sftp 挂载的服务器目录，这个终端不等于真实运行环境。  
②相关代码修改都要记录到 `开发文档/开发日志.md`。  

## Project Structure & Module Organization

- `qqbot/`：应用主体
- `qqbot/plugins/`：NoneBot 插件入口
- `qqbot/services/`：业务服务层
- `qqbot/core/`：配置、数据库、日志、调度器等基础设施
- `qqbot/models/`：SQLAlchemy 模型与动态分表约定
- `docker/`：Docker 部署文件
  - `docker/docker-compose.yml`：应用容器
  - `docker/postgres/compose.yml`：PostgreSQL
  - `docker/napcat/compose.yml`：NapCat
- `runtime_data/`：运行时本地缓存目录
- `logs/`：日志目录
- `tests/`：`unittest` 契约测试
- `开发文档/`：开发规范、数据库设计、开发日志等文档

## Build, Test, and Development Commands

- `pip install -e ".[dev]"`：安装运行与开发依赖
- `nb run --reload`：宿主机开发启动
- `nb run`：宿主机普通启动
- `docker compose -f docker/postgres/compose.yml up -d`：启动 PostgreSQL
- `docker compose -f docker/docker-compose.yml up -d --build`：启动应用容器
- `ruff check .`：Lint
- `ruff format .`：格式化
- `pyright`：类型检查
- `python -m unittest discover -s tests`：运行全部契约测试

## Database & Runtime Conventions

- 当前数据库后端是 PostgreSQL-only
- 宿主机默认数据库地址由根 `.env` 中的 `DATABASE_URL` 决定
- `docker/docker-compose.yml` 会在容器内覆盖 `DATABASE_URL` 为 `postgresql+asyncpg://admin:mypassword@postgres:5432/mydb`
- PostgreSQL 表结构由应用启动时自动创建
- `docker/postgres/init/` 当前为空
- 时间语义统一使用 `Asia/Shanghai` 的 timezone-aware `datetime`

## Coding Style & Naming Conventions

- Python 3.10+
- 公共函数必须带类型标注
- 仅使用异步 I/O，不要引入阻塞调用
- 命名规范：函数/变量 `snake_case`，类 `PascalCase`，常量 `UPPER_CASE`
- 日志使用模块级 logger，并在 `extra` 中带上下文

## Testing Guidelines

- 仓库已存在 `tests/` 下的 `unittest` 契约测试
- `qqbot/plugins/test_events.py` 只在 `QQBOT_ENABLE_TEST_EVENTS=1` 时加载
- 改动数据库、配置、提示词、部署语义时，要同步更新对应测试
- 当前终端没有完整运行环境，优先给出静态检查与契约测试结果

## Commit & Pull Request Guidelines

- 提交信息使用中文短前缀，例如 `修复：...`、`优化：...`、`更新：...`
- PR 说明要包含：改动摘要、验证方式、配置或数据库影响

## Configuration & Secrets

- 根 `.env` 是默认配置入口，`.env.example` 是脱敏模板
- 可选覆盖文件是 `.env.<ENVIRONMENT>`
- 不要提交真实 token、密码或其他密钥

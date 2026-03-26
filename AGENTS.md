# Repository Guidelines
说中文
①这个目录是通过sftp挂载的服务器上的目录 这个终端并没有上面程序的运行环境
②相关的代码修改都要记录到开发文档目录下的开发日志.md中
## Project Structure & Module Organization
`qqbot/` contains the application. `qqbot/plugins/` hosts NoneBot event handlers (priority-driven), `qqbot/services/` holds singleton business services, `qqbot/core/` contains infra (DB, LLM, scheduler), and `qqbot/models/` defines SQLAlchemy ORM models and sharded table naming. Root-level `postgres/` contains `docker-compose.yml` and `init.sql` for local DB. Config and docs live at `pyproject.toml`, `.env*`, `.env.example`, `README.md`, and `CLAUDE.md`.

## Build, Test, and Development Commands
- `pip install -e ".[dev]"` installs runtime + dev tools (ruff, pyright).
- `nb run --reload` starts the bot in development; `nb run` for production.
- `ruff check .` (lint) and `ruff format .` (format); `pyright` for type checking.
- `psql -U postgres -c "CREATE DATABASE qqbot;"` initializes DB; tables are auto-created by SQLAlchemy on startup.

## Coding Style & Naming Conventions
Python 3.10+ with Ruff enforcing 88-char lines and LF endings. Public functions require type hints; async I/O only (no blocking calls). Naming: `snake_case` for functions/vars, `PascalCase` for classes, `UPPER_CASE` for constants, and `snake_case_plural` for DB tables. Use `logging.getLogger(__name__)` and include context in `extra`.

## Testing Guidelines
No formal test suite is configured; `qqbot/plugins/test_events.py` is available for manual event checks only when `QQBOT_ENABLE_TEST_EVENTS=1`. Validate behavior by running `nb run --reload` and watching logs. If you add a test framework, document the commands and file layout here and in `pyproject.toml`.

## Commit & Pull Request Guidelines
Recent commits use short Chinese verb prefixes with a full-width colon, e.g. `修复：...`, `优化：...`, `更新：... - ...`. Keep summaries concise and mention key files or features. PRs should include a short description, testing/verification notes, and any config or DB-impacting changes. If dependencies change, record them in `CLAUDE.md`.

## Configuration & Secrets
Use `.env` for shared defaults, `.env.dev`/`.env.prod` for environment overrides, and `.env.example` as the checked-in template. Do not commit real tokens; keep example values and document required variables.

"""数据库配置和会话管理

提供异步数据库连接、会话工厂、分表操作等功能。
分表策略：
- 群成员表按 group_id 分表: group_members_{group_id}
- 群消息表按 group_id 分表: group_messages_v2_{group_id}
"""

from pathlib import Path
from typing import Any, AsyncGenerator

from sqlalchemy import event, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from pydantic_settings import BaseSettings

from qqbot.core.logging import get_logger
from qqbot.core.settings import get_settings_env_files

logger = get_logger(__name__)


class DatabaseConfig(BaseSettings):
    """数据库配置从环境变量读取"""

    database_url: str | None = None
    db_backend: str = "sqlite"
    db_host: str | None = None
    db_port: int | None = None
    db_user: str | None = None
    db_password: str | None = None
    db_name: str | None = None
    sqlite_path: str = "./sqlite_data/qqbot.db"
    db_echo: bool = False
    pool_size: int = 10
    max_overflow: int = 20

    class Config:
        env_file = get_settings_env_files()
        env_file_encoding = "utf-8"
        case_sensitive = False
        extra = "ignore"

    @property
    def resolved_database_url(self) -> str:
        """构建数据库URL"""
        if self.database_url:
            return self._normalize_database_url(self.database_url)

        if self.db_backend.lower() == "sqlite":
            sqlite_url = f"sqlite+aiosqlite:///{self.sqlite_path}"
            return self._normalize_database_url(sqlite_url)

        required_values = {
            "DB_HOST": self.db_host,
            "DB_PORT": self.db_port,
            "DB_USER": self.db_user,
            "DB_PASSWORD": self.db_password,
            "DB_NAME": self.db_name,
        }
        missing_keys = [key for key, value in required_values.items() if value in (None, "")]
        if missing_keys:
            missing = ", ".join(missing_keys)
            raise ValueError(
                f"Missing database config: {missing}. "
                "Please configure DATABASE_URL or complete DB_* settings in env files."
            )

        postgres_url = (
            f"postgresql+asyncpg://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )
        return self._normalize_database_url(postgres_url)

    @property
    def dialect_name(self) -> str:
        """当前数据库方言名称。"""
        return make_url(self.resolved_database_url).get_backend_name()

    @property
    def is_sqlite(self) -> bool:
        """当前是否使用 SQLite。"""
        return self.dialect_name == "sqlite"

    @staticmethod
    def _normalize_database_url(database_url: str) -> str:
        """规范化数据库 URL，确保使用异步驱动。"""
        normalized = database_url.strip()

        if normalized.startswith("postgres://"):
            return normalized.replace("postgres://", "postgresql+asyncpg://", 1)

        if normalized.startswith("postgresql://"):
            return normalized.replace("postgresql://", "postgresql+asyncpg://", 1)

        if normalized.startswith("sqlite:///"):
            return normalized.replace("sqlite:///", "sqlite+aiosqlite:///", 1)

        if normalized.startswith("sqlite://") and "+aiosqlite" not in normalized:
            return normalized.replace("sqlite://", "sqlite+aiosqlite://", 1)

        return normalized


# 全局数据库配置
config = DatabaseConfig()
sqlite_path: Path | None = None

if config.is_sqlite:
    sqlite_database = make_url(config.resolved_database_url).database
    if sqlite_database and sqlite_database != ":memory:":
        sqlite_path = Path(sqlite_database).expanduser()
        if not sqlite_path.is_absolute():
            sqlite_path = (Path.cwd() / sqlite_path).resolve()
        sqlite_path.parent.mkdir(parents=True, exist_ok=True)

# 调试：打印实际加载的配置
logger.info("[database] 📋 Database Config Loaded:")
logger.info(f"[database]   DIALECT: {config.dialect_name}")
if config.is_sqlite:
    logger.info(f"[database]   SQLITE_PATH: {sqlite_path or ':memory:'}")
else:
    logger.info(f"[database]   DB_HOST: {config.db_host}")
    logger.info(f"[database]   DB_PORT: {config.db_port}")
    logger.info(f"[database]   DB_USER: {config.db_user}")
    logger.info(f"[database]   DB_NAME: {config.db_name}")


def _build_engine_kwargs() -> dict[str, Any]:
    """根据当前数据库方言构建引擎参数。"""
    kwargs: dict[str, Any] = {"echo": config.db_echo}

    if config.is_sqlite:
        kwargs["connect_args"] = {"check_same_thread": False}
        return kwargs

    kwargs["pool_size"] = config.pool_size
    kwargs["max_overflow"] = config.max_overflow
    kwargs["connect_args"] = {
        "server_settings": {
            "application_name": "qqbot",
            "timezone": "Asia/Shanghai",
        }
    }
    return kwargs


# 创建异步引擎
engine: AsyncEngine = create_async_engine(
    config.resolved_database_url,
    **_build_engine_kwargs(),
)

if config.is_sqlite:
    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_connection: Any, _: Any) -> None:
        """为 SQLite 连接设置更适合机器人写入场景的 PRAGMA。"""
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

# 会话工厂
AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """获取数据库会话

    用于依赖注入（NoneBot2 Depends）

    Yields:
        AsyncSession: 数据库会话
    """
    async with AsyncSessionLocal() as session:
        yield session


async def init_db() -> None:
    """初始化数据库表

    创建所有静态表（users, groups, 模板表）
    """
    try:
        # 导入所有模型以注册到Base.metadata
        from qqbot.models import Base  # noqa: F401
        from qqbot.models.image import ImageRecord  # noqa: F401
        from qqbot.models.messages import User, Group, GroupMemberTemplate, GroupMessage  # noqa: F401

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await _ensure_image_records_columns(conn)
            logger.info("Database tables initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        raise


async def close_db() -> None:
    """关闭数据库连接"""
    try:
        await engine.dispose()
        logger.info("Database connection closed")
    except Exception as e:
        logger.error(f"Failed to close database connection: {e}")


async def table_exists(table_name: str) -> bool:
    """检查表是否存在

    Args:
        table_name: 表名

    Returns:
        bool: 表是否存在
    """
    async with engine.begin() as conn:
        return await conn.run_sync(
            lambda sync_conn: inspect(sync_conn).has_table(table_name)
        )


async def create_group_tables(group_id: int) -> tuple[str, str]:
    """为指定群组创建分表

    创建以下两个表：
    - group_members_{group_id}: 群成员表
    - group_messages_v2_{group_id}: 群消息表

    Args:
        group_id: 群组ID

    Returns:
        tuple[str, str]: (members_table_name, messages_table_name)

    Raises:
        Exception: 表创建失败
    """
    members_table = f"group_members_{group_id}"
    messages_table = f"group_messages_v2_{group_id}"

    try:
        async with engine.begin() as conn:
            logger.debug(f"[create_group_tables] Creating tables for group {group_id}")

            # 创建群成员表（如果不存在）
            await conn.execute(text(_build_members_table_sql(members_table)))
            logger.debug(f"[create_group_tables] Created/verified members table: {members_table}")

            # 创建群成员表索引（如果不存在）
            await conn.execute(text(f"""
                CREATE INDEX IF NOT EXISTS idx_{members_table}_user_id ON {members_table}(user_id)
            """))

            # 创建群消息表（如果不存在）
            await conn.execute(text(_build_messages_table_sql(messages_table)))
            logger.debug(f"[create_group_tables] Created/verified messages table: {messages_table}")

            # 创建群消息表索引（如果不存在）
            await conn.execute(text(f"""
                CREATE INDEX IF NOT EXISTS idx_{messages_table}_user_id ON {messages_table}(user_id)
            """))

            column_names = await conn.run_sync(
                lambda sync_conn: {
                    column["name"]
                    for column in inspect(sync_conn).get_columns(messages_table)
                }
            )
            if "onebot_message_id" not in column_names:
                await conn.execute(text(f"""
                    ALTER TABLE {messages_table}
                    ADD COLUMN onebot_message_id VARCHAR(255)
                """))
                column_names.add("onebot_message_id")

            if "message_id" in column_names:
                if config.is_sqlite:
                    await conn.execute(text(f"""
                        UPDATE {messages_table}
                        SET onebot_message_id = CAST(message_id AS TEXT)
                        WHERE onebot_message_id IS NULL
                          AND message_id IS NOT NULL
                    """))
                else:
                    await conn.execute(text(f"""
                        UPDATE {messages_table}
                        SET onebot_message_id = message_id::text
                        WHERE onebot_message_id IS NULL
                          AND message_id IS NOT NULL
                    """))

            await conn.execute(text(f"""
                CREATE INDEX IF NOT EXISTS idx_{messages_table}_is_recalled ON {messages_table}(is_recalled)
            """))

            await conn.execute(text(f"""
                CREATE INDEX IF NOT EXISTS idx_{messages_table}_onebot_message_id ON {messages_table}(onebot_message_id)
            """))

            await conn.execute(text(f"""
                CREATE INDEX IF NOT EXISTS idx_{messages_table}_timestamp ON {messages_table}("timestamp")
            """))

        logger.info(
            f"[create_group_tables] ✅ Group tables ready: {members_table}, {messages_table}"
        )
        return members_table, messages_table

    except Exception as e:
        logger.error(
            f"[create_group_tables] ❌ Failed to create/verify group tables for {group_id}: {e}",
            exc_info=True,
        )
        raise


def get_database_backend() -> str:
    """返回当前数据库方言名称。"""
    return config.dialect_name


def is_sqlite_backend() -> bool:
    """返回当前是否使用 SQLite。"""
    return config.is_sqlite


def _build_members_table_sql(table_name: str) -> str:
    """根据方言生成群成员动态表 DDL。"""
    if config.is_sqlite:
        return f"""
            CREATE TABLE IF NOT EXISTS {table_name} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id BIGINT NOT NULL UNIQUE,
                card VARCHAR(255),
                join_time DATETIME,
                is_active BOOLEAN DEFAULT TRUE,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            )
        """

    return f"""
        CREATE TABLE IF NOT EXISTS {table_name} (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL UNIQUE,
            card VARCHAR(255),
            join_time TIMESTAMP WITH TIME ZONE,
            is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """


def _build_messages_table_sql(table_name: str) -> str:
    """根据方言生成群消息动态表 DDL。"""
    if config.is_sqlite:
        return f"""
            CREATE TABLE IF NOT EXISTS {table_name} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id BIGINT NOT NULL,
                onebot_message_id VARCHAR(255),
                raw_message TEXT,
                formatted_message TEXT,
                is_recalled BOOLEAN DEFAULT FALSE,
                "timestamp" DATETIME NOT NULL
            )
        """

    return f"""
        CREATE TABLE IF NOT EXISTS {table_name} (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            onebot_message_id VARCHAR(255),
            raw_message TEXT,
            formatted_message TEXT,
            is_recalled BOOLEAN DEFAULT FALSE,
            "timestamp" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """


async def _ensure_image_records_columns(conn: Any) -> None:
    has_table = await conn.run_sync(
        lambda sync_conn: inspect(sync_conn).has_table("image_records")
    )
    if not has_table:
        return

    column_names = await conn.run_sync(
        lambda sync_conn: {
            column["name"]
            for column in inspect(sync_conn).get_columns("image_records")
        }
    )
    if "local_path" in column_names:
        return

    await conn.execute(text("ALTER TABLE image_records ADD COLUMN local_path TEXT"))

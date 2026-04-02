import importlib
from typing import Any, AsyncGenerator

from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
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
    database_url: str | None = None
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
        if self.database_url:
            return self._normalize_database_url(self.database_url)

        raise ValueError("Missing DATABASE_URL. Please configure PostgreSQL DATABASE_URL.")

    @property
    def dialect_name(self) -> str:
        """当前数据库方言名称。"""
        return make_url(self.resolved_database_url).get_backend_name()

    @staticmethod
    def _normalize_database_url(database_url: str) -> str:
        normalized = database_url.strip()

        if normalized.startswith("postgres://"):
            return normalized.replace("postgres://", "postgresql+asyncpg://", 1)

        if normalized.startswith("postgresql://"):
            return normalized.replace("postgresql://", "postgresql+asyncpg://", 1)

        if normalized.startswith("postgresql+asyncpg://"):
            return normalized

        if normalized.startswith(("sqlite://", "sqlite+aiosqlite://")):
            raise ValueError("SQLite is no longer supported. Please configure PostgreSQL.")

        raise ValueError(
            "Unsupported database URL. Please configure a PostgreSQL DATABASE_URL."
        )


# 全局数据库配置
config = DatabaseConfig()
database_url = make_url(config.resolved_database_url)

# 调试：打印实际加载的配置
logger.info("[database] 📋 Database Config Loaded:")
logger.info(f"[database]   DIALECT: {config.dialect_name}")
logger.info(f"[database]   DB_HOST: {database_url.host}")
logger.info(f"[database]   DB_PORT: {database_url.port}")
logger.info(f"[database]   DB_USER: {database_url.username}")
logger.info(f"[database]   DB_NAME: {database_url.database}")


def _build_engine_kwargs() -> dict[str, Any]:
    return {
        "echo": config.db_echo,
        "pool_size": config.pool_size,
        "max_overflow": config.max_overflow,
        "connect_args": {
            "server_settings": {
                "application_name": "qqbot",
                "timezone": "Asia/Shanghai",
            }
        },
    }


# 创建异步引擎
engine: AsyncEngine = create_async_engine(
    config.resolved_database_url,
    **_build_engine_kwargs(),
)

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
        importlib.import_module("qqbot.models.messages")
        importlib.import_module("qqbot.models.tool_call")

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
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


async def create_group_tables(
    group_id: int,
    conn: AsyncConnection | None = None,
) -> tuple[str, str]:
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
        if conn is None:
            async with engine.begin() as managed_conn:
                return await create_group_tables(group_id, managed_conn)

        logger.debug(f"[create_group_tables] Creating tables for group {group_id}")

        await conn.execute(text(_build_members_table_sql(members_table)))
        logger.debug(f"[create_group_tables] Created/verified members table: {members_table}")

        await conn.execute(text(f"""
            CREATE INDEX IF NOT EXISTS idx_{members_table}_user_id ON {members_table}(user_id)
        """))

        await conn.execute(text(_build_messages_table_sql(messages_table)))
        logger.debug(f"[create_group_tables] Created/verified messages table: {messages_table}")

        await conn.execute(text(f"""
            CREATE INDEX IF NOT EXISTS idx_{messages_table}_user_id ON {messages_table}(user_id)
        """))

        await conn.execute(text(f"""
            CREATE INDEX IF NOT EXISTS idx_{messages_table}_msg_hash ON {messages_table}(msg_hash)
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


def _build_members_table_sql(table_name: str) -> str:
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
    return f"""
        CREATE TABLE IF NOT EXISTS {table_name} (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            msg_hash VARCHAR(64) UNIQUE NOT NULL,
            onebot_message_id VARCHAR(255),
            raw_message TEXT,
            formatted_message TEXT,
            is_recalled BOOLEAN DEFAULT FALSE,
            "timestamp" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """

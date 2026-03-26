"""将 QQBot 数据从 PostgreSQL 迁移到 SQLite。"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import socket
from pathlib import Path
import sys
from typing import Any

from sqlalchemy import event, inspect, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from qqbot.core.time import normalize_china_time
from qqbot.models import Base

logger = logging.getLogger(__name__)

TIME_COLUMNS = {"created_at", "updated_at", "join_time", "timestamp"}

USERS_COLUMNS = [
    "id",
    "user_id",
    "nickname",
    "created_at",
    "updated_at",
]
GROUPS_COLUMNS = [
    "id",
    "group_id",
    "group_name",
    "table_name",
    "members_table_name",
    "created_at",
    "updated_at",
]
GROUP_MEMBERS_COLUMNS = [
    "id",
    "user_id",
    "card",
    "join_time",
    "is_active",
    "created_at",
    "updated_at",
]
GROUP_MESSAGES_COLUMNS = [
    "id",
    "user_id",
    "onebot_message_id",
    "raw_message",
    "formatted_message",
    "is_recalled",
    "timestamp",
]


def normalize_database_url(database_url: str) -> str:
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


def sqlite_url_from_path(sqlite_path: str) -> str:
    """将文件路径转换为 SQLite URL。"""
    return normalize_database_url(f"sqlite+aiosqlite:///{sqlite_path}")


def quote_identifier(identifier: str) -> str:
    """对动态表名做白名单校验后再用于 SQL。"""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", identifier):
        raise ValueError(f"非法表名: {identifier}")
    return f'"{identifier}"'


def build_insert_sql(table_name: str, columns: list[str]) -> str:
    """构造 INSERT 语句。"""
    quoted_table = quote_identifier(table_name)
    quoted_columns = ", ".join(quote_identifier(column) for column in columns)
    values_clause = ", ".join(f":{column}" for column in columns)
    return (
        f"INSERT INTO {quoted_table} ({quoted_columns}) "
        f"VALUES ({values_clause})"
    )


def build_select_sql(table_name: str, columns: list[str]) -> str:
    """构造按主键递增读取的 SELECT 语句。"""
    quoted_table = quote_identifier(table_name)
    quoted_columns = ", ".join(quote_identifier(column) for column in columns)
    return (
        f"SELECT {quoted_columns} "
        f"FROM {quoted_table} "
        f"ORDER BY id "
        f"LIMIT :limit OFFSET :offset"
    )


def build_count_sql(table_name: str) -> str:
    """构造统计行数的 SQL。"""
    return f"SELECT COUNT(*) FROM {quote_identifier(table_name)}"


def build_index_name(index_name: str) -> str:
    """校验并返回索引名。"""
    return quote_identifier(index_name)


def create_source_engine(source_url: str) -> AsyncEngine:
    """创建 PostgreSQL 源库连接。"""
    return create_async_engine(source_url)


def create_target_engine(target_url: str) -> AsyncEngine:
    """创建 SQLite 目标库连接。"""
    engine = create_async_engine(
        target_url,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_connection: Any, _: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    return engine


async def create_sqlite_schema(target_conn: AsyncConnection) -> None:
    """初始化静态表结构。"""
    await target_conn.run_sync(Base.metadata.create_all)


async def create_sqlite_group_tables(
    target_conn: AsyncConnection,
    members_table_name: str,
    messages_table_name: str,
) -> None:
    """在 SQLite 中创建群动态表。"""
    members_index_name = build_index_name(f"idx_{members_table_name}_user_id")
    messages_user_index_name = build_index_name(f"idx_{messages_table_name}_user_id")
    messages_recalled_index_name = build_index_name(
        f"idx_{messages_table_name}_is_recalled"
    )
    messages_onebot_id_index_name = build_index_name(
        f"idx_{messages_table_name}_onebot_message_id"
    )
    messages_timestamp_index_name = build_index_name(
        f"idx_{messages_table_name}_timestamp"
    )

    await target_conn.execute(text(f"""
        CREATE TABLE IF NOT EXISTS {quote_identifier(members_table_name)} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id BIGINT NOT NULL UNIQUE,
            card VARCHAR(255),
            join_time DATETIME,
            is_active BOOLEAN DEFAULT TRUE,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL
        )
    """))
    await target_conn.execute(text(f"""
        CREATE INDEX IF NOT EXISTS {members_index_name}
        ON {quote_identifier(members_table_name)}(user_id)
    """))
    await target_conn.execute(text(f"""
        CREATE TABLE IF NOT EXISTS {quote_identifier(messages_table_name)} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id BIGINT NOT NULL,
            onebot_message_id TEXT,
            raw_message TEXT,
            formatted_message TEXT,
            is_recalled BOOLEAN DEFAULT FALSE,
            "timestamp" DATETIME NOT NULL
        )
    """))
    await target_conn.execute(text(f"""
        CREATE INDEX IF NOT EXISTS {messages_user_index_name}
        ON {quote_identifier(messages_table_name)}(user_id)
    """))
    await target_conn.execute(text(f"""
        CREATE INDEX IF NOT EXISTS {messages_recalled_index_name}
        ON {quote_identifier(messages_table_name)}(is_recalled)
    """))
    await target_conn.execute(text(f"""
        CREATE INDEX IF NOT EXISTS {messages_onebot_id_index_name}
        ON {quote_identifier(messages_table_name)}(onebot_message_id)
    """))
    await target_conn.execute(text(f"""
        CREATE INDEX IF NOT EXISTS {messages_timestamp_index_name}
        ON {quote_identifier(messages_table_name)}("timestamp")
    """))


async def get_existing_columns(
    conn: AsyncConnection,
    table_name: str,
) -> list[str]:
    return await conn.run_sync(
        lambda sync_conn: [
            column["name"]
            for column in inspect(sync_conn).get_columns(table_name)
        ]
    )


async def fetch_rows(
    conn: AsyncConnection,
    table_name: str,
    columns: list[str],
    limit: int,
    offset: int,
) -> list[dict[str, Any]]:
    """分批获取源表数据。"""
    result = await conn.execute(
        text(build_select_sql(table_name, columns)),
        {"limit": limit, "offset": offset},
    )
    return [dict(row) for row in result.mappings().all()]


async def table_count(conn: AsyncConnection, table_name: str) -> int:
    """统计表行数。"""
    result = await conn.execute(text(build_count_sql(table_name)))
    return int(result.scalar() or 0)


async def copy_table_in_batches(
    source_conn: AsyncConnection,
    target_conn: AsyncConnection,
    table_name: str,
    columns: list[str],
    batch_size: int,
) -> int:
    """按批次复制表数据。"""
    insert_sql = text(build_insert_sql(table_name, columns))
    offset = 0
    copied = 0

    while True:
        rows = await fetch_rows(
            conn=source_conn,
            table_name=table_name,
            columns=columns,
            limit=batch_size,
            offset=offset,
        )
        if not rows:
            break

        for row in rows:
            for column in TIME_COLUMNS.intersection(row):
                row[column] = normalize_china_time(row[column])

        await target_conn.execute(insert_sql, rows)
        copied += len(rows)
        offset += batch_size
        logger.info("[migrate] %s copied=%s", table_name, copied)

    return copied


async def fetch_all_rows(
    conn: AsyncConnection,
    table_name: str,
    columns: list[str],
    batch_size: int,
) -> list[dict[str, Any]]:
    """分批读取整张表并合并结果。"""
    all_rows: list[dict[str, Any]] = []
    offset = 0

    while True:
        rows = await fetch_rows(
            conn=conn,
            table_name=table_name,
            columns=columns,
            limit=batch_size,
            offset=offset,
        )
        if not rows:
            break

        all_rows.extend(rows)
        offset += batch_size

    return all_rows


async def copy_message_table_in_batches(
    source_conn: AsyncConnection,
    target_conn: AsyncConnection,
    table_name: str,
    batch_size: int,
) -> int:
    source_columns = await get_existing_columns(source_conn, table_name)
    select_columns = [
        column
        for column in GROUP_MESSAGES_COLUMNS
        if column in source_columns
    ]

    legacy_message_id = "message_id" in source_columns
    insert_columns = list(select_columns)
    if legacy_message_id and "onebot_message_id" not in insert_columns:
        insert_columns.insert(2, "onebot_message_id")

    insert_sql = text(build_insert_sql(table_name, insert_columns))
    offset = 0
    copied = 0

    while True:
        rows = await fetch_rows(
            conn=source_conn,
            table_name=table_name,
            columns=select_columns + (["message_id"] if legacy_message_id else []),
            limit=batch_size,
            offset=offset,
        )
        if not rows:
            break

        if legacy_message_id:
            for row in rows:
                legacy_value = row.pop("message_id", None)
                row["onebot_message_id"] = (
                    str(legacy_value)
                    if legacy_value is not None
                    else None
                )

        for row in rows:
            for column in TIME_COLUMNS.intersection(row):
                row[column] = normalize_china_time(row[column])

        await target_conn.execute(insert_sql, rows)
        copied += len(rows)
        offset += batch_size
        logger.info("[migrate] %s copied=%s", table_name, copied)

    return copied


async def verify_table_counts(
    source_conn: AsyncConnection,
    target_conn: AsyncConnection,
    table_name: str,
) -> None:
    """校验迁移前后行数一致。"""
    source_count = await table_count(source_conn, table_name)
    target_count = await table_count(target_conn, table_name)
    if source_count != target_count:
        raise RuntimeError(
            f"表 {table_name} 行数不一致: source={source_count}, target={target_count}"
        )


async def migrate(
    source_url: str,
    sqlite_path: str,
    overwrite: bool,
    batch_size: int,
) -> None:
    """执行 PostgreSQL 到 SQLite 的迁移。"""
    source_engine = create_source_engine(normalize_database_url(source_url))

    target_path = Path(sqlite_path).expanduser()
    if not target_path.is_absolute():
        target_path = (Path.cwd() / target_path).resolve()
    target_path.parent.mkdir(parents=True, exist_ok=True)

    if target_path.exists():
        if not overwrite:
            raise FileExistsError(
                f"目标 SQLite 文件已存在: {target_path}，如需覆盖请加 --overwrite"
            )
        target_path.unlink()

    target_engine = create_target_engine(
        sqlite_url_from_path(target_path.as_posix())
    )

    try:
        try:
            source_conn_context = source_engine.connect()
            source_conn = await source_conn_context.__aenter__()
        except socket.gaierror as exc:
            raise RuntimeError(
                "无法解析 PostgreSQL 主机名。"
                "如果你是在宿主机终端执行脚本，请把 --pg-url 里的主机从容器名 "
                "`postgres16` 改成 `127.0.0.1` 或 `localhost`。"
            ) from exc

        try:
            async with target_engine.begin() as target_conn:
                await create_sqlite_schema(target_conn)

                await copy_table_in_batches(
                    source_conn=source_conn,
                    target_conn=target_conn,
                    table_name="users",
                    columns=USERS_COLUMNS,
                    batch_size=batch_size,
                )
                await verify_table_counts(source_conn, target_conn, "users")

                group_rows = await fetch_all_rows(
                    conn=source_conn,
                    table_name="groups",
                    columns=GROUPS_COLUMNS,
                    batch_size=batch_size,
                )
                if group_rows:
                    for row in group_rows:
                        for column in TIME_COLUMNS.intersection(row):
                            row[column] = normalize_china_time(row[column])
                    await target_conn.execute(
                        text(build_insert_sql("groups", GROUPS_COLUMNS)),
                        group_rows,
                    )
                await verify_table_counts(source_conn, target_conn, "groups")

                for group in group_rows:
                    group_id = group["group_id"]
                    members_table_name = group["members_table_name"]
                    messages_table_name = group["table_name"]

                    await create_sqlite_group_tables(
                        target_conn=target_conn,
                        members_table_name=members_table_name,
                        messages_table_name=messages_table_name,
                    )

                    await copy_table_in_batches(
                        source_conn=source_conn,
                        target_conn=target_conn,
                        table_name=members_table_name,
                        columns=GROUP_MEMBERS_COLUMNS,
                        batch_size=batch_size,
                    )
                    await verify_table_counts(
                        source_conn,
                        target_conn,
                        members_table_name,
                    )

                    await copy_message_table_in_batches(
                        source_conn=source_conn,
                        target_conn=target_conn,
                        table_name=messages_table_name,
                        batch_size=batch_size,
                    )
                    await verify_table_counts(
                        source_conn,
                        target_conn,
                        messages_table_name,
                    )

                    logger.info("[migrate] group=%s completed", group_id)

            logger.info("[migrate] all tables migrated to %s", target_path)
        finally:
            await source_conn_context.__aexit__(None, None, None)
    finally:
        await source_engine.dispose()
        await target_engine.dispose()


def build_argument_parser() -> argparse.ArgumentParser:
    """构造命令行参数解析器。"""
    parser = argparse.ArgumentParser(
        description="将 QQBot 数据从 PostgreSQL 迁移到 SQLite",
    )
    parser.add_argument(
        "--pg-url",
        required=True,
        help="源 PostgreSQL URL，例如 postgresql+asyncpg://user:pass@host:5432/qqbot",
    )
    parser.add_argument(
        "--sqlite-path",
        default="./sqlite_data/qqbot.db",
        help="目标 SQLite 文件路径，默认 ./sqlite_data/qqbot.db",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="每批复制的行数，默认 500",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="如果目标 SQLite 文件已存在则覆盖",
    )
    return parser


def main() -> None:
    """脚本入口。"""
    logging.basicConfig(level=logging.INFO)
    args = build_argument_parser().parse_args()
    asyncio.run(
        migrate(
            source_url=args.pg_url,
            sqlite_path=args.sqlite_path,
            overwrite=args.overwrite,
            batch_size=args.batch_size,
        )
    )


if __name__ == "__main__":
    main()

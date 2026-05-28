"""Contract: 仓库已彻底切到 PostgreSQL-only，不留任何 SQLite 残余。

v1 service / image_parsing / context 等模块已删除，相关 sqlite 分支断言
跟着删；保留 database.py 自身的清洁性 + pyproject 无 aiosqlite + 旧迁移
脚本不存在等仍然有意义的项。
"""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PostgresOnlyContractTests(unittest.TestCase):
    def read_text(self, relative_path: str) -> str:
        return (ROOT / relative_path).read_text(encoding="utf-8")

    def test_database_core_is_postgres_only(self) -> None:
        database_text = self.read_text("qqbot/core/database.py")

        self.assertIn("SQLite is no longer supported", database_text)
        self.assertNotIn('db_backend: str = "sqlite"', database_text)
        self.assertNotIn("sqlite_path", database_text)
        self.assertNotIn("def is_sqlite_backend", database_text)
        self.assertNotIn("config.is_sqlite", database_text)
        self.assertNotIn("db_host: str | None", database_text)
        self.assertNotIn("db_port: int | None", database_text)
        self.assertNotIn("db_user: str | None", database_text)
        self.assertNotIn("db_password: str | None", database_text)
        self.assertNotIn("db_name: str | None", database_text)
        self.assertNotIn("complete DB_* settings", database_text)
        self.assertNotIn("message_id::text", database_text)
        # v1 group dynamic sharding helpers 已随 v1 删除
        self.assertNotIn("def create_group_tables", database_text)
        self.assertNotIn("_build_members_table_sql", database_text)
        self.assertNotIn("_build_messages_table_sql", database_text)

    def test_docker_layout_baseline(self) -> None:
        postgres_compose = self.read_text("docker/postgres/compose.yml")
        gitignore = self.read_text(".gitignore")

        self.assertFalse((ROOT / "docker" / "Dockerfile").exists())
        self.assertFalse((ROOT / "docker" / "docker-compose.yml").exists())
        self.assertIn("network_mode: bridge", postgres_compose)
        self.assertNotIn("qqbot-postgres-network", postgres_compose)
        # runtime_data/ 必须被 .gitignore 屏蔽（heartbeat 等本地状态落在那里）。
        # 此前还要求 README 提及 runtime_data/，但 README 现在是用户面文档不再
        # 暴露这类内部细节；.gitignore 才是事实标准，足以表达契约。
        self.assertIn("runtime_data/", gitignore)

    def test_sqlite_dependency_and_migrations_removed(self) -> None:
        pyproject = self.read_text("pyproject.toml")

        self.assertNotIn("aiosqlite", pyproject)
        # v1 数据库迁移脚本与 v1 service 数据库模块均不应再存在
        self.assertFalse((ROOT / "qqbot" / "services" / "database.py").exists())
        self.assertFalse((ROOT / "scripts" / "migrate_postgres_to_sqlite.py").exists())
        self.assertFalse((ROOT / "scripts" / "migrate_group_messages_v2.py").exists())
        self.assertFalse((ROOT / "scripts" / "migrate_group_messages_v2.sql").exists())
        self.assertFalse((ROOT / "scripts" / "remove_message_id_column.sql").exists())
        self.assertFalse(
            (ROOT / "scripts" / "migrate_unified_tool_calls.py").exists()
        )


if __name__ == "__main__":
    unittest.main()

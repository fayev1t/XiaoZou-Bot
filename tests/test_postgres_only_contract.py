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
        self.assertNotIn("ALTER TABLE {messages_table}", database_text)
        self.assertNotIn("message_id::text", database_text)
        self.assertNotIn("_ensure_image_records_columns", database_text)

    def test_services_no_longer_branch_on_sqlite(self) -> None:
        for relative_path in (
            "qqbot/services/user.py",
            "qqbot/services/group.py",
            "qqbot/services/group_message.py",
        ):
            text = self.read_text(relative_path)
            with self.subTest(file=relative_path):
                self.assertNotIn("is_sqlite_backend", text)
                self.assertNotIn("dialects.sqlite", text)
                self.assertNotIn("last_insert_rowid", text)

    def test_runtime_cache_path_no_longer_uses_sqlite_name(self) -> None:
        image_parsing = self.read_text("qqbot/services/image_parsing.py")
        dockerfile = self.read_text("docker/Dockerfile")
        docker_compose = self.read_text("docker/docker-compose.yml")
        postgres_compose = self.read_text("docker/postgres/compose.yml")
        gitignore = self.read_text(".gitignore")
        dockerignore = self.read_text("docker/.dockerignore")
        readme = self.read_text("README.md")

        self.assertIn('Path("./runtime_data/images")', image_parsing)
        self.assertNotIn("sqlite_data", image_parsing)
        self.assertIn("/app/runtime_data/images", dockerfile)
        self.assertIn('VOLUME ["/app/logs", "/app/runtime_data"]', dockerfile)
        self.assertIn('EXPOSE 7500', dockerfile)
        self.assertIn("../runtime_data:/app/runtime_data", docker_compose)
        self.assertIn("../logs:/app/logs", docker_compose)
        self.assertIn("context: ..", docker_compose)
        self.assertIn("dockerfile: docker/Dockerfile", docker_compose)
        self.assertIn("postgres:5432", docker_compose)
        self.assertIn("name: qqbot-postgres-network", docker_compose)
        self.assertIn("name: qqbot-postgres-network", postgres_compose)
        self.assertIn("runtime_data/", gitignore)
        self.assertIn("runtime_data", dockerignore)
        self.assertIn("runtime_data/", readme)

    def test_sqlite_dependency_and_script_are_removed(self) -> None:
        pyproject = self.read_text("pyproject.toml")
        context = self.read_text("qqbot/services/context.py")

        self.assertNotIn("aiosqlite", pyproject)
        self.assertNotIn('msg.get("message_content"', context)
        self.assertFalse((ROOT / "qqbot" / "services" / "database.py").exists())
        self.assertFalse((ROOT / "scripts" / "migrate_postgres_to_sqlite.py").exists())
        self.assertFalse((ROOT / "scripts" / "migrate_group_messages_v2.py").exists())
        self.assertFalse((ROOT / "scripts" / "migrate_group_messages_v2.sql").exists())
        self.assertFalse((ROOT / "scripts" / "remove_message_id_column.sql").exists())


if __name__ == "__main__":
    unittest.main()

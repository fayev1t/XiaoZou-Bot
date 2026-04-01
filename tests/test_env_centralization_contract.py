from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class EnvCentralizationContractTests(unittest.TestCase):
    def read_text(self, relative_path: str) -> str:
        return (ROOT / relative_path).read_text(encoding="utf-8")

    def test_env_example_defaults_to_postgres_and_documents_docker_settings(self) -> None:
        env_example = self.read_text(".env.example")

        self.assertIn(
            "DATABASE_URL=postgresql+asyncpg://admin:your_postgres_password@127.0.0.1:7504/mydb",
            env_example,
        )
        self.assertIn("PORT=7500", env_example)
        self.assertNotIn("sqlite+aiosqlite", env_example)
        self.assertNotIn("sqlite", env_example.lower())
        self.assertNotIn("NAPCAT_UID=", env_example)
        self.assertNotIn("NAPCAT_GID=", env_example)

        for key in (
            "NAPCAT_WEBUI_HOST_PORT=7501",
            "NAPCAT_HTTP_HOST_PORT=7502",
            "NAPCAT_WS_HOST_PORT=7503",
            "POSTGRES_USER=admin",
            "POSTGRES_PASSWORD=your_postgres_password",
            "POSTGRES_DB=mydb",
            "POSTGRES_HOST_PORT=7504",
        ):
            with self.subTest(key=key):
                self.assertIn(key, env_example)

    def test_app_compose_explicitly_reads_root_env_file(self) -> None:
        compose_text = self.read_text("docker/docker-compose.yml")

        self.assertIn("      context: ..", compose_text)
        self.assertIn("      dockerfile: docker/Dockerfile", compose_text)
        self.assertIn("      - ../.env", compose_text)
        self.assertIn('    user: "1001:1001"', compose_text)
        self.assertIn(
            "      DATABASE_URL: postgresql+asyncpg://admin:mypassword@postgres:5432/mydb",
            compose_text,
        )
        self.assertIn('      - "7500:7500"', compose_text)
        self.assertIn("      - ../logs:/app/logs", compose_text)
        self.assertIn("      - ../runtime_data:/app/runtime_data", compose_text)
        self.assertIn("    name: qqbot-postgres-network", compose_text)

    def test_napcat_compose_keeps_current_hardcoded_ports_and_permissions(self) -> None:
        compose_text = self.read_text("docker/napcat/compose.yml")

        self.assertNotIn("env_file:", compose_text)
        self.assertIn('"7501:6099"', compose_text)
        self.assertIn('"7502:3000"', compose_text)
        self.assertIn('"7503:3001"', compose_text)
        self.assertNotIn("NAPCAT_WEBUI_HOST_PORT", compose_text)
        self.assertNotIn("NAPCAT_HTTP_HOST_PORT", compose_text)
        self.assertNotIn("NAPCAT_WS_HOST_PORT", compose_text)
        self.assertIn("NAPCAT_UID=${UID}", compose_text)
        self.assertIn("NAPCAT_GID=${GID}", compose_text)
        self.assertNotIn('user: "1001:1001"', compose_text)

    def test_postgres_compose_keeps_current_hardcoded_contract(self) -> None:
        compose_text = self.read_text("docker/postgres/compose.yml")

        self.assertNotIn("env_file:", compose_text)
        self.assertIn("POSTGRES_USER: admin", compose_text)
        self.assertIn("POSTGRES_PASSWORD: mypassword", compose_text)
        self.assertIn("POSTGRES_DB: mydb", compose_text)
        self.assertIn('    user: "1001:1001"', compose_text)
        self.assertIn('"7504:5432"', compose_text)
        self.assertIn(
            'test: ["CMD-SHELL", "pg_isready -U admin -d mydb"]',
            compose_text,
        )
        self.assertIn("    name: qqbot-postgres-network", compose_text)
        self.assertNotIn("${POSTGRES_USER}", compose_text)
        self.assertNotIn("${POSTGRES_PASSWORD}", compose_text)
        self.assertNotIn("${POSTGRES_DB}", compose_text)
        self.assertNotIn("${POSTGRES_HOST_PORT}", compose_text)


if __name__ == "__main__":
    unittest.main()

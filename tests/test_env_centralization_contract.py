from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class EnvCentralizationContractTests(unittest.TestCase):
    def read_text(self, relative_path: str) -> str:
        return (ROOT / relative_path).read_text(encoding="utf-8")

    def test_service_compose_layout_is_repo_baseline(self) -> None:
        for relative_path in (
            "docker/postgres/compose.yml",
            "docker/napcat/compose.yml",
            "docker/searxng/compose.yml",
            "docker/crawl4ai/compose.yml",
        ):
            with self.subTest(path=relative_path):
                self.assertTrue((ROOT / relative_path).exists())

        self.assertFalse((ROOT / "docker" / "docker-compose.yml").exists())
        self.assertFalse((ROOT / "docker" / "Dockerfile").exists())

    def test_pyproject_no_longer_depends_on_crawl4ai(self) -> None:
        pyproject_text = self.read_text("pyproject.toml")

        self.assertNotIn('"crawl4ai', pyproject_text)

    def test_env_example_defaults_to_postgres_and_documents_docker_settings(self) -> None:
        env_example = self.read_text(".env.example")

        self.assertIn(
            "DATABASE_URL=postgresql+asyncpg://admin:your_postgres_password@your_docker_service_host:7504/mydb",
            env_example,
        )
        self.assertIn("PORT=7500", env_example)
        self.assertNotIn("sqlite+aiosqlite", env_example)
        self.assertNotIn("sqlite", env_example.lower())

        for key in (
            "NAPCAT_WEBUI_HOST_PORT=7501",
            "NAPCAT_HTTP_HOST_PORT=7502",
            "NAPCAT_WS_HOST_PORT=7503",
            "POSTGRES_USER=admin",
            "POSTGRES_PASSWORD=your_postgres_password",
            "POSTGRES_DB=mydb",
            "POSTGRES_HOST_PORT=7504",
            "SEARXNG_BASE_URL=http://your_docker_service_host:7505",
            "SEARXNG_SECRET=change-this-searxng-secret",
            "CRAWL4AI_BASE_URL=http://your_docker_service_host:7506",
        ):
            with self.subTest(key=key):
                self.assertIn(key, env_example)

    def test_napcat_compose_keeps_current_hardcoded_ports_and_permissions(self) -> None:
        compose_text = self.read_text("docker/napcat/compose.yml")

        self.assertNotIn("env_file:", compose_text)
        self.assertNotIn("version:", compose_text)
        self.assertIn('"7501:6099"', compose_text)
        self.assertIn('"7502:3000"', compose_text)
        self.assertIn('"7503:3001"', compose_text)
        self.assertNotIn("NAPCAT_WEBUI_HOST_PORT", compose_text)
        self.assertNotIn("NAPCAT_HTTP_HOST_PORT", compose_text)
        self.assertNotIn("NAPCAT_WS_HOST_PORT", compose_text)
        self.assertIn("NAPCAT_UID: ${UID}", compose_text)
        self.assertIn("NAPCAT_GID: ${GID}", compose_text)
        self.assertNotIn('user: "1001:1001"', compose_text)
        self.assertIn("network_mode: bridge", compose_text)
        self.assertIn('      - "./QQ:/app/.config/QQ"', compose_text)
        self.assertNotIn("\nnetworks:\n", compose_text)
        self.assertNotIn("qqbot-napcat-network", compose_text)

    def test_postgres_compose_keeps_current_hardcoded_contract(self) -> None:
        compose_text = self.read_text("docker/postgres/compose.yml")

        self.assertNotIn("env_file:", compose_text)
        self.assertNotIn("version:", compose_text)
        self.assertIn("image: postgres:18-alpine", compose_text)
        self.assertIn("container_name: postgres18_qqbot", compose_text)
        self.assertIn("POSTGRES_USER: admin", compose_text)
        self.assertIn("POSTGRES_PASSWORD: mypassword", compose_text)
        self.assertIn("POSTGRES_DB: mydb", compose_text)
        self.assertNotIn('    user: "1001:1001"', compose_text)
        self.assertIn('"7504:5432"', compose_text)
        self.assertIn('"./postgres-data:/var/lib/postgresql"', compose_text)
        self.assertIn(
            'test: ["CMD-SHELL", "pg_isready -U admin -d mydb"]',
            compose_text,
        )
        self.assertIn("network_mode: bridge", compose_text)
        self.assertNotIn("\nnetworks:\n", compose_text)
        self.assertNotIn("qqbot-postgres-network", compose_text)

    def test_searxng_compose_keeps_current_hardcoded_contract(self) -> None:
        compose_text = self.read_text("docker/searxng/compose.yml")

        self.assertNotIn("env_file:", compose_text)
        self.assertNotIn("version:", compose_text)
        self.assertIn("image: searxng/searxng:latest", compose_text)
        self.assertIn("container_name: searxng_qqbot", compose_text)
        self.assertIn("restart: unless-stopped", compose_text)
        self.assertIn("SEARXNG_BASE_URL: http://127.0.0.1:7505", compose_text)
        self.assertIn("SEARXNG_SECRET: ${SEARXNG_SECRET:-change-this-searxng-secret}", compose_text)
        self.assertIn('SEARXNG_LIMITER: "false"', compose_text)
        self.assertIn('SEARXNG_PUBLIC_INSTANCE: "false"', compose_text)
        self.assertIn('"7505:8080"', compose_text)
        self.assertIn('"./config:/etc/searxng"', compose_text)
        self.assertIn('"./cache:/var/cache/searxng"', compose_text)
        self.assertNotIn("searxng-config:", compose_text)
        self.assertNotIn("searxng-cache:", compose_text)
        self.assertIn("network_mode: bridge", compose_text)
        self.assertNotIn("\nnetworks:\n", compose_text)
        self.assertNotIn("qqbot-searxng-network", compose_text)

    def test_crawl4ai_compose_exposes_http_service_contract(self) -> None:
        compose_text = self.read_text("docker/crawl4ai/compose.yml")

        self.assertIn("image: unclecode/crawl4ai:basic", compose_text)
        self.assertIn("container_name: crawl4ai_qqbot", compose_text)
        self.assertIn("restart: unless-stopped", compose_text)
        self.assertIn('"7506:11235"', compose_text)
        self.assertIn('shm_size: "1g"', compose_text)
        self.assertIn("TZ: Asia/Shanghai", compose_text)
        self.assertIn("network_mode: bridge", compose_text)
        self.assertNotIn("version:", compose_text)
        self.assertNotIn("\nnetworks:\n", compose_text)
        self.assertNotIn("qqbot-crawl4ai-network", compose_text)


if __name__ == "__main__":
    unittest.main()

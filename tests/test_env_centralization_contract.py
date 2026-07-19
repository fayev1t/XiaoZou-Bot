from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class EnvCentralizationContractTests(unittest.TestCase):
    def read_text(self, relative_path: str) -> str:
        return (ROOT / relative_path).read_text(encoding="utf-8")

    def test_service_compose_layout_is_repo_baseline(self) -> None:
        # searxng / crawl4ai 已随 websearch 工具 Tavily 化下线（2026-07-18），
        # 不再是基线的一部分；其 compose 目录暂留仓库仅为部署侧执行
        # `docker compose down`，之后手动删除，这里不再约束其存在。
        for relative_path in (
            "docker/postgres/compose.yml",
            "docker/napcat/compose.yml",
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
            "WEBSEARCH_PROVIDER=exa",
            "TAVILY_API_KEY=",
            # Prompt 快照（待办 #11）：模板默认开启采集（代码默认关闭，
            # 由部署侧显式决定）
            "PROMPT_SNAPSHOT_ENABLED=true",
            "PROMPT_SNAPSHOT_DIR=./runtime_data/prompt_snapshots",
            "PROMPT_SNAPSHOT_KEEP=200",
            "PROMPT_SNAPSHOT_SCOPES=group,system",
        ):
            with self.subTest(key=key):
                self.assertIn(key, env_example)

        # websearch 的 SearXNG + Crawl4AI 容器方案已下线（2026-07-18 Tavily
        # 化），env 模板不应再出现两者的配置（防回潮，同 sqlite 断言风格）。
        self.assertNotIn("SEARXNG", env_example)
        self.assertNotIn("CRAWL4AI", env_example)

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
        # NAPCAT_UID/GID 的 env-var 注入曾经是设想，但当前 compose 不再依赖
        # 宿主机 UID/GID（也不再硬编码 user: "1001:1001"）。这里仅约束这两种
        # 旧形态都不应回潮。
        self.assertNotIn("NAPCAT_UID:", compose_text)
        self.assertNotIn("NAPCAT_GID:", compose_text)
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


if __name__ == "__main__":
    unittest.main()

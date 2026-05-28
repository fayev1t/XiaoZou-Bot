"""Contract for the v2 main plugin (qqbot.plugins.v2_main).

Static-only. Verifies the plugin is wired up to:
- import EventIngest + mapper registry
- register message / notice / request / metaevent handlers at priority=10 block=True
- register bot to bot_registry inside every handler
- delegate heartbeat to EventIngest internal bypass
- swallow ingest exceptions so napcat doesn't retry-spin
- launch LoopSupervisor on startup, stop on shutdown
- be discoverable by both __main__ PLUGIN_MODULES and pyproject plugin_dirs
- v1 plugins MUST NOT appear in PLUGIN_MODULES (v1 fully discarded)
"""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class V2MainPluginContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plugin_text = (
            ROOT / "qqbot" / "plugins" / "v2_main.py"
        ).read_text(encoding="utf-8")
        self.main_text = (ROOT / "qqbot" / "__main__.py").read_text(encoding="utf-8")
        self.pyproject_text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.ingest_text = (
            ROOT / "qqbot" / "services" / "event_ingest" / "ingest.py"
        ).read_text(encoding="utf-8")

    def test_plugin_imports_event_ingest(self) -> None:
        self.assertIn(
            "from qqbot.services.event_ingest import EventIngest", self.plugin_text
        )
        self.assertIn(
            "from qqbot.services.event_ingest.mappers import build_default_registry",
            self.plugin_text,
        )

    def test_plugin_imports_agent_loop_and_tools(self) -> None:
        self.assertIn("LLMPlanner", self.plugin_text)
        self.assertIn("LoopSupervisor", self.plugin_text)
        self.assertIn("bot_registry", self.plugin_text)
        self.assertIn(
            "from qqbot.services.agent_loop.tools import build_default_registry",
            self.plugin_text,
        )

    def test_plugin_uses_async_session_local(self) -> None:
        self.assertIn(
            "from qqbot.core.database import AsyncSessionLocal", self.plugin_text
        )
        self.assertIn("session_factory=AsyncSessionLocal", self.plugin_text)

    def test_plugin_registers_all_four_handler_types_at_priority_10_block_true(self) -> None:
        # v2 是唯一消费者：block=True 保证事件不会被任何其他 matcher 二次处理。
        self.assertIn("on_message(priority=10, block=True)", self.plugin_text)
        self.assertIn("on_notice(priority=10, block=True)", self.plugin_text)
        self.assertIn("on_request(priority=10, block=True)", self.plugin_text)
        self.assertIn("on_metaevent(priority=10, block=True)", self.plugin_text)

    def test_handlers_register_bot_to_registry(self) -> None:
        # ReplySendWorker / ToolWorker 依赖 bot_registry 反查 Bot 实例
        self.assertIn("bot_registry.register(bot)", self.plugin_text)
        self.assertIn("_remember_bot(bot)", self.plugin_text)

    def test_ingest_handles_heartbeat_via_bypass(self) -> None:
        # heartbeat 不入 agent_events，走文件旁路（EventIngest契约 §7）
        self.assertIn("write_heartbeat", self.ingest_text)
        self.assertIn('"heartbeat"', self.ingest_text)
        self.assertIn("meta_event_type", self.ingest_text)

    def test_plugin_swallows_handler_exceptions(self) -> None:
        self.assertIn("except Exception", self.plugin_text)
        self.assertIn("swallowed", self.plugin_text)

    def test_plugin_loads_persona_into_llm_planner(self) -> None:
        # qqbot/persona.md 是主人设文件，必须存在且非空
        persona_path = ROOT / "qqbot" / "persona.md"
        self.assertTrue(persona_path.exists())
        persona_text = persona_path.read_text(encoding="utf-8").strip()
        self.assertTrue(len(persona_text) > 0)
        # v2_main 必须读取 persona.md 并注入 LLMPlanner
        self.assertIn("persona.md", self.plugin_text)
        self.assertIn("_load_persona_text", self.plugin_text)
        self.assertIn("persona_text=persona_text", self.plugin_text)

    def test_plugin_starts_and_stops_supervisor(self) -> None:
        self.assertIn("@_driver.on_startup", self.plugin_text)
        self.assertIn("@_driver.on_shutdown", self.plugin_text)
        self.assertIn("supervisor", self.plugin_text)
        self.assertIn(".start()", self.plugin_text)
        self.assertIn(".stop()", self.plugin_text)

    def test_no_legacy_toggle_env_vars(self) -> None:
        # v1 已删，过渡 env 开关也跟着删掉
        self.assertNotIn("QQBOT_V2_INGEST_ENABLED", self.plugin_text)
        self.assertNotIn("QQBOT_V2_LOOP_ENABLED", self.plugin_text)

    def test_plugin_listed_in_main_module_list(self) -> None:
        self.assertIn('"qqbot.plugins.v2_main"', self.main_text)

    def test_main_does_not_load_v1_plugins(self) -> None:
        # v1 三个 plugin 必须从 PLUGIN_MODULES 移除
        self.assertNotIn("event_handlers", self.main_text)
        self.assertNotIn("group_chat", self.main_text)
        self.assertNotIn("friend_private", self.main_text)
        self.assertNotIn("sync_nicknames", self.main_text)

    def test_pyproject_plugin_dirs_covers_qqbot_plugins(self) -> None:
        self.assertIn('plugin_dirs = ["qqbot/plugins"]', self.pyproject_text)


if __name__ == "__main__":
    unittest.main()

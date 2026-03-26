import os

import nonebot

from qqbot.core.logging import logger
from qqbot.core.settings import get_runtime_environment

PLUGIN_MODULES = [
    "qqbot.plugins.startup",
    "qqbot.plugins.event_handlers",
    "qqbot.plugins.group_chat",
    "qqbot.plugins.friend_private",
]


def _should_enable_test_events() -> bool:
    raw_value = os.getenv("QQBOT_ENABLE_TEST_EVENTS", "")
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}

logger.info("🚀 Starting QQ Bot with loguru logging...")

nonebot.init()

for plugin_module in PLUGIN_MODULES:
    nonebot.load_plugin(plugin_module)

if _should_enable_test_events():
    nonebot.load_plugin("qqbot.plugins.test_events")
    logger.info(
        "[startup] Debug test_events plugin enabled",
        extra={"environment": get_runtime_environment()},
    )

import nonebot.plugin
logger.info(f"[startup] Loaded plugins: {nonebot.plugin.get_loaded_plugins()}")


if __name__ == "__main__":
    nonebot.run()

import nonebot

# Initialize loguru logging system
from qqbot.core.logging import logger

logger.info("🚀 Starting QQ Bot with loguru logging...")

nonebot.init()

# Load all plugins explicitly
nonebot.load_plugins("qqbot.plugins")

# Print loaded plugins
import nonebot.plugin
logger.info(f"[startup] Loaded plugins: {nonebot.plugin.get_loaded_plugins()}")


if __name__ == "__main__":
    nonebot.run()

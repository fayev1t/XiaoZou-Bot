"""Startup plugin —— 初始化 DB + scheduler。

v2 唯一路径，已不再依赖 v1 的 message_aggregator / silence_mode /
sync_nicknames 等模块。AgentLoop 由 qqbot.plugins.v2_main 拉起。
"""

from nonebot import get_driver

from qqbot.core.logging import get_logger

logger = get_logger(__name__)

driver = get_driver()


@driver.on_startup
async def on_startup() -> None:
    """Initialize database and scheduler on startup."""
    logger.info("🚀 *** STARTUP PLUGIN TRIGGERED ***")
    logger.info("=" * 50)
    logger.info("🚀 Initializing core services...")
    logger.info("=" * 50)

    try:
        from qqbot.core import init_db, init_scheduler

        logger.info("[startup] 📦 Initializing database...")
        await init_db()
        logger.info("[startup] ✅ Database initialized")

        logger.info("[startup] ⏱️  Initializing scheduler...")
        await init_scheduler()
        logger.info("[startup] ✅ Scheduler initialized")

        logger.info("=" * 50)
        logger.info("[startup] ✨ Core services ready!")
        logger.info("=" * 50)

    except Exception as e:
        logger.exception("[startup] ❌ Initialization failed: {}", e)
        raise


@driver.on_shutdown
async def on_shutdown() -> None:
    """Cleanup on shutdown."""
    logger.info("[shutdown] 🛑 Shutting down...")
    try:
        from qqbot.core import shutdown_scheduler, close_db

        await shutdown_scheduler()
        await close_db()
        logger.info("[shutdown] ✅ Cleanup complete")
    except Exception as e:
        logger.exception("[shutdown] ❌ Error during cleanup: {}", e)

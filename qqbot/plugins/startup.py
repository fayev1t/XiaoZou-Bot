"""Startup plugin - Initialize database, scheduler and sync tasks.

This plugin is loaded by NoneBot and handles all startup initialization.
"""

import asyncio
from nonebot import get_bots, get_driver

from qqbot.core.logging import get_logger
from qqbot.services.message_aggregator import message_aggregator
from qqbot.services.silence_mode import reset_silence_states

logger = get_logger(__name__)

driver = get_driver()
INITIAL_SYNC_RETRY_SECONDS = 5
INITIAL_SYNC_RETRY_ATTEMPTS = 24


@driver.on_startup
async def on_startup() -> None:
    """Initialize database and scheduler on startup."""
    logger.info("🚀 *** STARTUP PLUGIN TRIGGERED ***")
    logger.info("=" * 50)
    logger.info("🚀 Initializing core services...")
    logger.info("=" * 50)

    try:
        from qqbot.core import init_db, init_scheduler

        # Initialize database
        logger.info("[startup] 📦 Initializing database...")
        await init_db()
        logger.info("[startup] ✅ Database initialized")

        # Initialize scheduler
        logger.info("[startup] ⏱️  Initializing scheduler...")
        await init_scheduler()
        logger.info("[startup] ✅ Scheduler initialized")

        logger.info("=" * 50)
        logger.info("[startup] ✨ Core services ready!")
        logger.info("=" * 50)

        logger.info("[startup] ⏰ Scheduling nickname sync jobs...")
        _register_sync_jobs()
        asyncio.create_task(_run_initial_sync_when_ready())

    except Exception as e:
        logger.error(f"[startup] ❌ Initialization failed: {e}", exc_info=True)
        raise


def _register_sync_jobs() -> None:
    from qqbot.core.scheduler import get_scheduler

    scheduler = get_scheduler()
    logger.debug(f"scheduler.running = {scheduler.running}")

    if not scheduler.running:
        logger.error("❌ Scheduler not running")
        return

    scheduler.add_job(
        _run_sync_nicknames_job,
        "interval",
        minutes=30,
        id="sync_nicknames_periodic",
        replace_existing=True,
    )
    logger.info("✅ Registered periodic sync (every 30 minutes)")


async def _run_initial_sync_when_ready() -> None:
    logger.info("🚀 *** _run_initial_sync_when_ready STARTED ***")

    for attempt in range(1, INITIAL_SYNC_RETRY_ATTEMPTS + 1):
        if not get_bots():
            logger.info(
                "[startup] Waiting for connected bots before initial sync",
                extra={"attempt": attempt, "max_attempts": INITIAL_SYNC_RETRY_ATTEMPTS},
            )
            await asyncio.sleep(INITIAL_SYNC_RETRY_SECONDS)
            continue

        await _run_sync_nicknames_job()
        return

    logger.warning("[startup] No bots connected before initial sync retry window ended")


async def _run_sync_nicknames_job() -> None:
    from qqbot.plugins.sync_nicknames import sync_all_group_nicknames

    connected_bots = list(get_bots().values())
    if not connected_bots:
        logger.warning("[startup] Skip nickname sync because no bots are connected")
        return

    for bot in connected_bots:
        try:
            await sync_all_group_nicknames(bot)
        except Exception as e:
            logger.error(
                f"[startup] Failed nickname sync for bot {getattr(bot, 'self_id', None)}: {e}",
                exc_info=True,
            )

    logger.info(
        "[startup] Nickname sync finished",
        extra={"bot_count": len(connected_bots)},
    )


@driver.on_shutdown
async def on_shutdown() -> None:
    """Cleanup on shutdown."""
    logger.info("[shutdown] 🛑 Shutting down...")
    try:
        from qqbot.core import shutdown_scheduler, close_db

        await message_aggregator.shutdown()
        reset_silence_states()
        await shutdown_scheduler()
        await close_db()
        logger.info("[shutdown] ✅ Cleanup complete")
    except Exception as e:
        logger.error(f"[shutdown] ❌ Error during cleanup: {e}", exc_info=True)

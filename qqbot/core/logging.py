"""Loguru-based logging configuration for the QQ bot.

This module provides a centralized logging setup using loguru with:
- Colored console output
- Rotating file logs
- Structured logging for AI model interactions
"""

import sys
from pathlib import Path

from loguru import logger

# Remove default handler
logger.remove()

# Console handler with colors
logger.add(
    sys.stderr,
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
    level="DEBUG",
    colorize=True,
)

# Create logs directory
LOGS_DIR = Path(__file__).parent.parent.parent / "logs"
LOGS_DIR.mkdir(exist_ok=True)

# General log file (rotating daily, keep 7 days)
logger.add(
    LOGS_DIR / "qqbot_{time:YYYY-MM-DD}.log",
    format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
    level="INFO",
    rotation="00:00",
    retention="7 days",
    encoding="utf-8",
)

# AI interactions log (separate file for model I/O)
logger.add(
    LOGS_DIR / "ai_interactions_{time:YYYY-MM-DD}.log",
    format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {message}",
    level="DEBUG",
    rotation="00:00",
    retention="14 days",
    encoding="utf-8",
    filter=lambda record: record["extra"].get("ai_log", False),
)

# Error log (separate file for errors)
logger.add(
    LOGS_DIR / "errors_{time:YYYY-MM-DD}.log",
    format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}\n{exception}",
    level="ERROR",
    rotation="00:00",
    retention="30 days",
    encoding="utf-8",
    backtrace=True,
    diagnose=True,
)


def get_logger(name: str):
    """Get a logger instance with the given name.
    
    Args:
        name: Logger name (usually __name__)
        
    Returns:
        Configured loguru logger
    """
    return logger.bind(name=name)


def log_ai_input(layer: str, group_id: int, prompt: str) -> None:
    """Log AI model input.
    
    Args:
        layer: Which layer is calling (Layer1/Layer2/Layer3)
        group_id: Group ID
        prompt: The prompt sent to AI
    """
    logger.bind(ai_log=True).info(
        f"[{layer}] 📤 AI INPUT | group={group_id}\n"
        f"{'='*60}\n{prompt}\n{'='*60}"
    )


def log_ai_output(layer: str, group_id: int, response: str) -> None:
    """Log AI model output.
    
    Args:
        layer: Which layer is calling (Layer1/Layer2/Layer3)
        group_id: Group ID
        response: The response from AI
    """
    logger.bind(ai_log=True).info(
        f"[{layer}] 📥 AI OUTPUT | group={group_id}\n"
        f"{'='*60}\n{response}\n{'='*60}"
    )


def log_event(event_type: str, group_id: int | None = None, user_id: int | None = None, **kwargs) -> None:
    """Log a special event.
    
    Args:
        event_type: Type of event (e.g., "message_received", "block_created")
        group_id: Group ID if applicable
        user_id: User ID if applicable
        **kwargs: Additional event data
    """
    extra_info = " | ".join(f"{k}={v}" for k, v in kwargs.items())
    location = f"group={group_id}" if group_id else ""
    if user_id:
        location += f" user={user_id}"
    
    logger.info(f"🔔 EVENT [{event_type}] | {location} | {extra_info}")

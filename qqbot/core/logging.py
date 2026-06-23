"""Loguru-based logging configuration for the QQ bot.

This module provides a centralized logging setup using loguru with:
- Colored console output
- Rotating file logs
"""

import sys
from pathlib import Path

from loguru import logger

# Remove default handler
logger.remove()


def _suppress_heartbeat_noise(record) -> bool:
    """丢掉 nonebot 每 3s 心跳引发的 matcher 生命周期日志洪水。

    心跳（meta_event）→ EventIngest 旁路、不写 DB，但 nonebot 内部仍把它当
    普通事件走完整套 matcher 流水线，每次发 5 行日志（"Event will be handled
    by ... type='meta_event'" / "Running Matcher" / "Running handler ... _on_meta"
    / "running complete" / "Stop event propagation"）。每 3 秒 5 行，几分钟就
    把 stderr / qqbot 日志文件冲烂，根本看不到真业务日志。

    过滤规则（只针对 nonebot logger，不影响业务侧）：
      - 消息文本里出现 type='meta_event' → 心跳 100% 触发
      - 消息文本里出现 _on_meta         → 我们自己的心跳 handler
      - 函数名 _handle_stop_propagation → 每个 block=True matcher 都喊一次，
        本身就属于纯调试噪音，统一抑掉（业务排障靠 INFO+ 日志足够）
    """
    if not record["name"].startswith("nonebot"):
        return True
    msg = record["message"]
    if "type='meta_event'" in msg or "_on_meta" in msg:
        return False
    if record["function"] == "_handle_stop_propagation":
        return False
    return True


# Console handler with colors
logger.add(
    sys.stderr,
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
    level="DEBUG",
    colorize=True,
    filter=_suppress_heartbeat_noise,
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
    filter=_suppress_heartbeat_noise,
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

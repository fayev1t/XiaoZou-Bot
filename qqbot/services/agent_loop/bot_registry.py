"""V2 自己的 nonebot Bot 实例缓存。

每个连接上来的 Bot 实例由 v2 ingest plugin handler 在事件触发时调
register() 写进这里；ToolWorker 与各同步工具（send_message / kick / ...）
通过 self_id 找回 Bot 实例来调 napcat API。

不复用 v1 (group_chat.py 里的 _bot_instances)：v2 完全自包含。

单进程内调用，threading.Lock 仅用于多线程安全的偏执；asyncio
单线程场景下基本无竞争。
"""

from __future__ import annotations

import threading
from typing import Any

_lock = threading.Lock()
_bots: dict[str, Any] = {}


def register(bot: Any) -> None:
    """每次 nonebot handler 触发都可以调一遍，同 self_id 反复写无副作用。"""
    self_id = str(getattr(bot, "self_id", "") or "")
    if not self_id:
        return
    with _lock:
        _bots[self_id] = bot


def get(self_id: str | int | None) -> Any | None:
    if self_id is None:
        return None
    with _lock:
        return _bots.get(str(self_id))


def get_any() -> Any | None:
    """随便拿一个 bot。单 bot 部署的便利接口。"""
    with _lock:
        if not _bots:
            return None
        return next(iter(_bots.values()))


def all_self_ids() -> list[str]:
    with _lock:
        return list(_bots.keys())


def clear() -> None:
    """仅供测试调用。"""
    with _lock:
        _bots.clear()

"""Core infrastructure for QQ Bot.

包级 __init__ **不再在 import 期** eager 拉 database / scheduler / llm —— 否则
`import qqbot.core`(哪怕只为取个常量 / 子模块)就会连带:
  - 构造 DB engine(database.py 模块级 `create_async_engine` + 读 DATABASE_URL)
  - 拉起 LLM(langchain)/调度器(apscheduler)全家桶

改用 PEP 562 module `__getattr__` 惰性解析:`qqbot.core.init_db` 等公开名首次
被【访问】时才导入对应子模块,`from qqbot.core import init_db` 语义完全不变。
(③ 模块解耦,见开发日志 2026-06-23。)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

# 公开名 → 所属子模块。__getattr__ 据此惰性导入。
_LAZY: dict[str, str] = {
    "init_db": "qqbot.core.database",
    "close_db": "qqbot.core.database",
    "get_db_session": "qqbot.core.database",
    "init_scheduler": "qqbot.core.scheduler",
    "shutdown_scheduler": "qqbot.core.scheduler",
    "get_scheduler": "qqbot.core.scheduler",
    "create_llm": "qqbot.core.llm",
}

__all__ = list(_LAZY)


def __getattr__(name: str):
    module_path = _LAZY.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    module = importlib.import_module(module_path)
    return getattr(module, name)


def __dir__() -> list[str]:
    return sorted(__all__)


if TYPE_CHECKING:  # 静态分析仍看得到真实符号(运行期才惰性解析)
    from qqbot.core.database import close_db, get_db_session, init_db
    from qqbot.core.llm import create_llm
    from qqbot.core.scheduler import (
        get_scheduler,
        init_scheduler,
        shutdown_scheduler,
    )

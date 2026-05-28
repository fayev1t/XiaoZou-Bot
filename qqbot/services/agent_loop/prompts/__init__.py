"""Prompt 片段加载工具。

工具模块用 `load_sibling_md(__file__, "x.md")` 取与代码就近放在同一目录下
的 markdown 用法说明，启动期校验路径但容错：文件缺失时返回空串并打一条
警告，不阻塞 plugin 加载。

文件搜索约定：
  - 第一参数传 `__file__`（调用方所在 .py 路径）
  - 第二参数是相对 sibling 文件名
真实路径取调用方目录 + 文件名 —— 与 v1 / persona.md 的相对寻址约定一致。
"""

from __future__ import annotations

from pathlib import Path

from qqbot.core.logging import get_logger

logger = get_logger(__name__)


def load_sibling_md(anchor_file: str, name: str) -> str:
    """读 anchor 同目录下的 markdown 文件；缺失 / 读失败返回空串。

    返回去除首尾空白的内容。空文件等同于不存在 —— 调用方不需要为 \"\" /
    None 写两套判断。
    """
    try:
        path = Path(anchor_file).resolve().parent / name
        if not path.exists():
            logger.warning("[prompts] {} missing at {}", name, path)
            return ""
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            logger.warning("[prompts] {} is empty at {}", name, path)
        return text
    except Exception as exc:
        logger.warning("[prompts] load {} failed: {}", name, exc)
        return ""


__all__ = ["load_sibling_md"]

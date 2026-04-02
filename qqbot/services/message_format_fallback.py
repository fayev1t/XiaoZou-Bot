from __future__ import annotations


def build_parse_failure_text(reason: str) -> str:
    normalized_reason = reason.strip() if reason else "消息不可用"
    return f"【解析失败：{normalized_reason}】"

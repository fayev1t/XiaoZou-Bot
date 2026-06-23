from __future__ import annotations

import os
import time
from uuid import uuid4


def new_msg_hash() -> str:
    return uuid4().hex


_CROCKFORD_BASE32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_event_id() -> str:
    """生成一枚 ULID 风格的事件 id（26 字符 Crockford base32）。

    时间分量 48 bit（毫秒）+ 随机分量 80 bit。
    用作 agent_events.event_id 主键，详见 开发文档/v2.0/事件系统设计.md §2、§3。
    """
    timestamp_ms = int(time.time() * 1000) & ((1 << 48) - 1)
    randomness = int.from_bytes(os.urandom(10), "big")
    value = (timestamp_ms << 80) | randomness

    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD_BASE32[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))

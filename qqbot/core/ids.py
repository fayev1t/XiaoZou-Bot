from __future__ import annotations

from uuid import uuid4


def new_msg_hash() -> str:
    return uuid4().hex


def new_call_hash() -> str:
    return uuid4().hex

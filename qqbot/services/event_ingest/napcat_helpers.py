"""Small duck-typed helpers for working with nonebot OneBot V11 events.

Mappers stay decoupled from concrete nonebot types by going through these
helpers; tests can pass plain SimpleNamespace fakes.
"""

from __future__ import annotations

from typing import Any


def dump_event(event: Any) -> dict:
    """Best-effort serialize a nonebot Event to a plain dict.

    Tries pydantic v2 (`model_dump`) first, then pydantic v1 (`dict`),
    then returns {} so ingest never crashes on a malformed event.
    """
    for attr in ("model_dump", "dict"):
        fn = getattr(event, attr, None)
        if callable(fn):
            try:
                return dict(fn())
            except Exception:
                pass
    return {}


def dump_segments(message: Any) -> list[dict]:
    """Serialize a nonebot Message (iterable of MessageSegment) to plain dicts."""
    if message is None:
        return []
    out: list[dict] = []
    for seg in message:
        if isinstance(seg, dict):
            out.append(seg)
            continue
        try:
            seg_type = getattr(seg, "type", None)
            seg_data = dict(getattr(seg, "data", {}) or {})
            out.append({"type": seg_type, "data": seg_data})
        except Exception:
            out.append({"type": "unknown", "data": {}})
    return out

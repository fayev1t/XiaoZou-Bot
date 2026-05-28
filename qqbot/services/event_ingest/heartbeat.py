"""Atomic write of napcat heartbeat to runtime_data/napcat_heartbeat.json.

EventIngest契约.md §7.1: heartbeat is NOT persisted into agent_events.
Only the most recent timestamp + status is kept on disk; readers (e.g. an
external watchdog) decide adapter liveness by file mtime + last_heartbeat_at.

Write strategy: tempfile in the same directory + fsync + os.replace.
On any error: log warning and continue; an out-of-band watchdog catches
stale mtime separately, so a single lost write is not catastrophic.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from qqbot.core.logging import get_logger
from qqbot.core.time import normalize_china_time

logger = get_logger(__name__)

HEARTBEAT_FILE = Path("./runtime_data/napcat_heartbeat.json")


def _normalize_status(status: Any) -> Any:
    if status is None or isinstance(status, (dict, str, int, float, bool)):
        return status
    for attr in ("model_dump", "dict"):
        fn = getattr(status, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:
                pass
    return str(status)


def serialize_heartbeat(event: Any) -> dict:
    last_at = normalize_china_time(getattr(event, "time", None)).isoformat()
    return {
        "self_id": getattr(event, "self_id", None),
        "last_heartbeat_at": last_at,
        "interval_ms": getattr(event, "interval", None),
        "status": _normalize_status(getattr(event, "status", None)),
    }


def _write_sync(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".heartbeat-", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


async def write_heartbeat(event: Any, path: Path | None = None) -> None:
    """Atomic snapshot write; never raises (failures are logged)."""
    target = path or HEARTBEAT_FILE
    payload = serialize_heartbeat(event)
    try:
        await asyncio.to_thread(_write_sync, target, payload)
    except Exception as exc:
        logger.warning("[heartbeat] write failed: {}", exc)

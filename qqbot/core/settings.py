from __future__ import annotations

import json
import os
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ENVIRONMENT_ALIASES: dict[str, tuple[str, ...]] = {
    "dev": ("development",),
    "development": ("dev",),
    "prod": ("production",),
    "production": ("prod",),
}


def _read_environment_from_dotenv(env_path: Path) -> str | None:
    if not env_path.exists():
        return None

    try:
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            if key.strip() != "ENVIRONMENT":
                continue

            cleaned = value.strip().strip('"').strip("'")
            return cleaned or None
    except OSError:
        return None

    return None


def get_runtime_environment() -> str:
    raw_environment = (
        os.getenv("ENVIRONMENT")
        or _read_environment_from_dotenv(_REPO_ROOT / ".env")
        or "dev"
    )
    normalized = raw_environment.strip().lower()
    return normalized or "dev"


def get_settings_env_files() -> tuple[str, ...]:
    env_files: list[str] = []

    shared_env = _REPO_ROOT / ".env"
    if shared_env.exists():
        env_files.append(str(shared_env))

    environment = get_runtime_environment()
    candidates = dict.fromkeys((environment, *_ENVIRONMENT_ALIASES.get(environment, ())))
    for candidate in candidates:
        candidate_path = _REPO_ROOT / f".env.{candidate}"
        if candidate_path.exists():
            env_files.append(str(candidate_path))

    return tuple(env_files)


def _read_env_value_from_files(key: str) -> str | None:
    value: str | None = None
    for env_file in get_settings_env_files():
        env_path = Path(env_file)
        if not env_path.exists():
            continue

        try:
            for raw_line in env_path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue

                current_key, current_value = line.split("=", 1)
                if current_key.strip() != key:
                    continue

                value = current_value.strip()
        except OSError:
            continue

    return value


def get_env_value(key: str) -> str | None:
    if key in os.environ:
        return os.environ[key]

    raw_value = _read_env_value_from_files(key)
    if raw_value is None:
        return None

    return raw_value.strip().strip('"').strip("'")


def get_bot_nicknames() -> tuple[str, ...]:
    raw_value = get_env_value("NICKNAME")
    if not raw_value:
        return ("小奏",)

    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError:
        parsed = raw_value

    if isinstance(parsed, list):
        names = tuple(
            str(item).strip()
            for item in parsed
            if str(item).strip()
        )
        return names or ("小奏",)

    if isinstance(parsed, str) and parsed.strip():
        return (parsed.strip(),)

    return ("小奏",)


def get_primary_bot_name() -> str:
    return get_bot_nicknames()[0]

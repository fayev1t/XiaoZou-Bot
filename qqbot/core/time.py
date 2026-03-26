from datetime import datetime
from zoneinfo import ZoneInfo

CHINA_TIMEZONE = ZoneInfo("Asia/Shanghai")


def china_now() -> datetime:
    return datetime.now(CHINA_TIMEZONE).replace(tzinfo=None)


def normalize_china_time(value: datetime | int | float | None) -> datetime:
    if value is None:
        return china_now()

    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value
        return value.astimezone(CHINA_TIMEZONE).replace(tzinfo=None)

    return datetime.fromtimestamp(value, CHINA_TIMEZONE).replace(tzinfo=None)

"""Compatibilidade exclusiva do dispatcher legado local.

O GenericWorker não importa este módulo. Em produção a única autoridade é
``machine_admin.scheduling``, baseada no convênio persistido no PostgreSQL.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from services.registry import MUNICIPALITIES, PLATFORMS, platform_for_municipality


TIMEZONE = ZoneInfo("America/Fortaleza")
START_HOUR = 7
START_HOUR_BY_PLATFORM = {
    slug: platform.start_hour for slug, platform in PLATFORMS.items()
}
END_HOUR_BY_PLATFORM = {
    slug: platform.end_hour for slug, platform in PLATFORMS.items()
}
PLATFORM_BY_PREFEITURA = {
    slug: municipality.platform_slug
    for slug, municipality in MUNICIPALITIES.items()
}


def platform_for(prefeitura: str) -> str:
    return platform_for_municipality(prefeitura).slug


def is_within_window(platform: str, moment: datetime | None = None) -> bool:
    moment = _local_moment(moment)
    if moment.weekday() >= 5:
        return False
    end_hour = END_HOUR_BY_PLATFORM[platform]
    return START_HOUR_BY_PLATFORM[platform] <= moment.hour < end_hour


def next_start_time(platform: str, moment: datetime | None = None) -> datetime:
    moment = _local_moment(moment)
    if is_within_window(platform, moment):
        return moment

    candidate_day = moment.date()
    if moment.hour >= END_HOUR_BY_PLATFORM[platform] or moment.weekday() >= 5:
        candidate_day += timedelta(days=1)
    while candidate_day.weekday() >= 5:
        candidate_day += timedelta(days=1)
    return datetime.combine(
        candidate_day, time(START_HOUR_BY_PLATFORM[platform]), tzinfo=TIMEZONE
    )


def _local_moment(moment: datetime | None) -> datetime:
    if moment is None:
        return datetime.now(TIMEZONE)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=TIMEZONE)
    return moment.astimezone(TIMEZONE)

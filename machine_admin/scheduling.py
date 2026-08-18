"""Política de agenda persistida e explicável por convênio."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from machine_admin.models import Municipality, Platform


@dataclass(frozen=True, slots=True)
class ScheduleDecision:
    allowed: bool
    reason: str
    next_start_at: datetime | None
    timezone: str
    weekdays: tuple[int, ...]
    start_hour: int
    end_hour: int

    def as_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["next_start_at"] = (
            self.next_start_at.isoformat() if self.next_start_at else None
        )
        return value


def _timezone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "America/Fortaleza")
    except ZoneInfoNotFoundError:
        return ZoneInfo("America/Fortaleza")


def _policy(municipality: Municipality, platform: Platform) -> tuple[tuple[int, ...], int, int]:
    raw = getattr(municipality, "schedule_policy", None) or {}
    weekdays = tuple(
        sorted(
            {
                int(value)
                for value in raw.get("weekdays", [0, 1, 2, 3, 4])
                if str(value).isdigit() and 0 <= int(value) <= 6
            }
        )
    )
    start = raw.get("start_hour")
    end = raw.get("end_hour")
    start_hour = platform.start_hour if start is None else int(start)
    end_hour = platform.end_hour if end is None else int(end)
    return weekdays, max(0, min(start_hour, 23)), max(1, min(end_hour, 24))


def schedule_decision(
    municipality: Municipality,
    platform: Platform,
    *,
    moment: datetime | None = None,
) -> ScheduleDecision:
    timezone_name = getattr(municipality, "timezone", None) or "America/Fortaleza"
    timezone = _timezone(timezone_name)
    if moment is None:
        local = datetime.now(timezone)
    elif moment.tzinfo is None:
        local = moment.replace(tzinfo=timezone)
    else:
        local = moment.astimezone(timezone)
    weekdays, start_hour, end_hour = _policy(municipality, platform)
    allowed = local.weekday() in weekdays and start_hour <= local.hour < end_hour
    if allowed:
        return ScheduleDecision(
            True,
            "Dentro da janela configurada.",
            None,
            timezone_name,
            weekdays,
            start_hour,
            end_hour,
        )

    candidate = local.replace(hour=start_hour, minute=0, second=0, microsecond=0)
    if local.weekday() not in weekdays or local >= candidate:
        candidate += timedelta(days=1)
    for _ in range(8):
        if candidate.weekday() in weekdays:
            break
        candidate += timedelta(days=1)
    reason = (
        "Dia não permitido pela agenda do convênio."
        if local.weekday() not in weekdays
        else "Fora do horário configurado."
    )
    return ScheduleDecision(
        False,
        reason,
        candidate.astimezone(UTC),
        timezone_name,
        weekdays,
        start_hour,
        end_hour,
    )

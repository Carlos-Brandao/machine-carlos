"""Política única de janelas para o scheduler dos robôs."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo


FORTALEZA_TZ = ZoneInfo("America/Fortaleza")
START_HOUR = 7

PREFEITURAS_POR_PLATAFORMA = {
    "rf1": {"boa-vista", "pref2"},
    "easyconsig": {"chapeco"},
    "safeconsig": {"fortaleza", "tamboril"},
    "facil": {"teresina", "gov-am", "paulista", "paulista-previdencia", "mossoro"},
}

# A hora final é exclusiva: SafeConsig pode trabalhar até 17:59; as demais,
# até 20:59. Todos só iniciam em dias úteis, a partir das 07:00.
END_HOUR_BY_PLATFORM = {
    "safeconsig": 18,
    "facil": 21,
    "rf1": 21,
    "fenix": 21,
    "grid": 21,
    "easyconsig": 21,
}


def platform_for(prefeitura: str) -> str:
    for platform, prefeituras in PREFEITURAS_POR_PLATAFORMA.items():
        if prefeitura in prefeituras:
            return platform
    raise ValueError(f"Prefeitura sem plataforma configurada: {prefeitura}")


def is_within_window(platform: str, now: datetime | None = None) -> bool:
    local_now = _local_now(now)
    return (
        local_now.weekday() < 5
        and START_HOUR <= local_now.hour < END_HOUR_BY_PLATFORM[platform]
    )


def next_start_time(platform: str, now: datetime | None = None) -> datetime:
    """Retorna o instante permitido para o próximo início, no fuso Fortaleza."""
    local_now = _local_now(now)
    if is_within_window(platform, local_now):
        return local_now

    candidate_date = local_now.date()
    if local_now.weekday() >= 5 or local_now.time() >= time(END_HOUR_BY_PLATFORM[platform]):
        candidate_date += timedelta(days=1)

    while candidate_date.weekday() >= 5:
        candidate_date += timedelta(days=1)

    return datetime.combine(candidate_date, time(START_HOUR), tzinfo=FORTALEZA_TZ)


def _local_now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(FORTALEZA_TZ)
    if now.tzinfo is None:
        return now.replace(tzinfo=FORTALEZA_TZ)
    return now.astimezone(FORTALEZA_TZ)

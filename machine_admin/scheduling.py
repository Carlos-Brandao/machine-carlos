"""Timezone-aware five-field cron schedules persisted in PostgreSQL."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from machine_admin.models import Job, Municipality, Platform, Schedule, ScheduleOccurrence
from machine_admin.operations import ACTIVE_JOB_STATES, create_execution, validate_execution_selection


def _field(expression: str, lower: int, upper: int) -> set[int]:
    values: set[int] = set()
    for part in expression.split(","):
        base, separator, step_text = part.partition("/")
        try:
            step = int(step_text) if separator else 1
            if step < 1:
                raise ValueError
            if base == "*":
                start, end = lower, upper
            elif "-" in base:
                start, end = map(int, base.split("-"))
            else:
                start = int(base)
                end = upper if separator else start
            if start < lower or end > upper or end < start:
                raise ValueError
            values.update(range(start, end + 1, step))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Campo cron inválido: {expression}.") from exc
    return values


def parse_cron(expression: str) -> tuple[list[str], list[set[int]]]:
    parts = expression.split()
    if len(parts) != 5:
        raise ValueError("Cron deve ter cinco campos: minuto hora dia mês dia-da-semana (0=domingo).")
    fields = [_field(part, lower, upper) for part, (lower, upper) in zip(parts, ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7)), strict=True)]
    fields[4] = {day % 7 for day in fields[4]}
    return parts, fields


def next_occurrence(expression: str, timezone: str, after: datetime) -> datetime:
    """Returns an actual UTC instant, skipping nonexistent local DST times."""
    parts, (minutes, hours, days, months, weekdays) = parse_cron(expression)
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("Fuso horário IANA inválido.") from exc
    if after.tzinfo is None:
        raise ValueError("O horário deve informar seu fuso.")
    after = after.astimezone(UTC)
    current_date = after.astimezone(zone).date()
    for offset in range(366 * 8):
        date = current_date + timedelta(days=offset)
        if date.month not in months:
            continue
        day_matches = date.day in days
        weekday_matches = (date.weekday() + 1) % 7 in weekdays
        # Standard cron: when both day fields are restricted either can match.
        if parts[2].startswith("*") or parts[4].startswith("*"):
            matches = day_matches and weekday_matches
        else:
            matches = day_matches or weekday_matches
        if not matches:
            continue
        candidates = []
        for hour in sorted(hours):
            for minute in sorted(minutes):
                local = datetime(date.year, date.month, date.day, hour, minute, tzinfo=zone)
                # Each wall-clock occurrence fires once on the first fold.
                moment = local.astimezone(UTC)
                if moment > after and moment.astimezone(zone).replace(tzinfo=None) == local.replace(tzinfo=None):
                    candidates.append(moment)
        if candidates:
            return min(candidates)
    raise ValueError("Cron não possui ocorrência válida nos próximos oito anos.")


def create_schedule(session: Session, *, name: str, dataset_id: int, requested_by_id: int | None, cron_expression: str, timezone: str = "America/Fortaleza", selected_credential_ids: list[int] | None = None, max_parallel_accounts: int = 1, enabled: bool = True, misfire_grace_seconds: int = 300) -> Schedule:
    _, accounts = validate_execution_selection(session, dataset_id=dataset_id, selected_credential_ids=selected_credential_ids, max_parallel_accounts=max_parallel_accounts, require_usable=False)
    name = name.strip()
    if not name or len(name) > 160:
        raise ValueError("Informe um nome de até 160 caracteres para o agendamento.")
    if not 60 <= misfire_grace_seconds <= 3600:
        raise ValueError("A tolerância de atraso deve ser de 60 a 3600 segundos.")
    schedule = Schedule(name=name, dataset_id=dataset_id, requested_by_id=requested_by_id,
        cron_expression=" ".join(cron_expression.split()), timezone=timezone,
        selected_credential_ids=accounts, max_parallel_accounts=max_parallel_accounts,
        enabled=enabled, misfire_grace_seconds=misfire_grace_seconds,
        next_run_at=next_occurrence(cron_expression, timezone, datetime.now(UTC)))
    session.add(schedule)
    session.flush()
    return schedule


def update_schedule(session: Session, schedule: Schedule, **changes) -> Schedule:
    allowed = {"name", "dataset_id", "cron_expression", "timezone", "selected_credential_ids", "max_parallel_accounts", "enabled", "misfire_grace_seconds"}
    if set(changes) - allowed:
        raise ValueError("Campo de agendamento desconhecido.")
    if changes == {"enabled": False}:
        schedule.enabled = False
        session.flush()
        return schedule
    # Validate a complete proposed configuration before changing the persisted one.
    values = {key: changes.get(key, getattr(schedule, key)) for key in allowed}
    if not values["name"].strip() or len(values["name"]) > 160 or not 60 <= values["misfire_grace_seconds"] <= 3600:
        raise ValueError("Nome ou tolerância de atraso inválidos.")
    _, accounts = validate_execution_selection(session, dataset_id=values["dataset_id"], selected_credential_ids=values["selected_credential_ids"], max_parallel_accounts=values["max_parallel_accounts"], require_usable=False)
    next_run = next_occurrence(values["cron_expression"], values["timezone"], datetime.now(UTC))
    for key, value in values.items():
        setattr(schedule, key, value)
    schedule.selected_credential_ids = accounts
    schedule.next_run_at = next_run
    session.flush()
    return schedule


def serialize_schedule(schedule: Schedule) -> dict:
    runs = []
    next_run = schedule.next_run_at
    if schedule.enabled:
        for _ in range(5):
            runs.append(next_run.isoformat())
            next_run = next_occurrence(schedule.cron_expression, schedule.timezone, next_run)
    fields = ("id", "name", "dataset_id", "cron_expression", "timezone", "selected_credential_ids", "max_parallel_accounts", "enabled", "misfire_grace_seconds")
    return {**{key: getattr(schedule, key) for key in fields}, "next_run_at": schedule.next_run_at.isoformat(), "last_run_at": schedule.last_run_at.isoformat() if schedule.last_run_at else None, "next_runs": runs, "overlap_policy": "skip", "missed_policy": "skip_outside_grace"}


def process_due_schedules(session: Session, *, now: datetime | None = None, limit: int = 25) -> int:
    now = now or datetime.now(UTC)
    schedules = list(session.scalars(select(Schedule).where(Schedule.enabled.is_(True), Schedule.next_run_at <= now).order_by(Schedule.next_run_at, Schedule.id).limit(limit).with_for_update(skip_locked=True)))
    for schedule in schedules:
        due = schedule.next_run_at
        # Advancing directly beyond now bounds recovery: no backlog storm after an outage.
        schedule.next_run_at = next_occurrence(schedule.cron_expression, schedule.timezone, now)
        schedule.last_run_at = now
        if session.scalar(select(ScheduleOccurrence.id).where(ScheduleOccurrence.schedule_id == schedule.id, ScheduleOccurrence.scheduled_for == due)):
            continue
        occurrence = ScheduleOccurrence(schedule_id=schedule.id, scheduled_for=due, status="created")
        session.add(occurrence)
        if (now - due).total_seconds() > schedule.misfire_grace_seconds:
            occurrence.status = "skipped_late"
            occurrence.message = "Horário perdido durante indisponibilidade; retomado no próximo disparo."
            continue
        running = session.scalar(select(Job.id).join(ScheduleOccurrence, ScheduleOccurrence.job_id == Job.id).where(ScheduleOccurrence.schedule_id == schedule.id, Job.status.in_(ACTIVE_JOB_STATES)).limit(1))
        if running:
            occurrence.status = "skipped_overlap"
            occurrence.message = f"A consulta #{running} deste agendamento ainda não terminou."
            continue
        try:
            with session.begin_nested():
                job = create_execution(session, dataset_id=schedule.dataset_id, requested_by_id=schedule.requested_by_id, selected_credential_ids=schedule.selected_credential_ids, max_parallel_accounts=schedule.max_parallel_accounts, idempotency_namespace=f"schedule:{schedule.id}", idempotency_key=due.isoformat())
            occurrence.job_id = job.id
            occurrence.message = "Consulta criada pelo agendamento."
        except ValueError as exc:
            occurrence.status = "blocked"
            occurrence.message = str(exc)
    session.flush()
    return len(schedules)


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
        value["next_start_at"] = self.next_start_at.isoformat() if self.next_start_at else None
        return value


def _timezone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "America/Fortaleza")
    except ZoneInfoNotFoundError:
        return ZoneInfo("America/Fortaleza")


def _policy(municipality: Municipality, platform: Platform) -> tuple[tuple[int, ...], int, int]:
    raw = getattr(municipality, "schedule_policy", None) or {}
    weekdays = tuple(sorted({int(value) for value in raw.get("weekdays", [0, 1, 2, 3, 4]) if str(value).isdigit() and 0 <= int(value) <= 6}))
    start, end = raw.get("start_hour"), raw.get("end_hour")
    start_hour = platform.start_hour if start is None else int(start)
    end_hour = platform.end_hour if end is None else int(end)
    return weekdays, max(0, min(start_hour, 23)), max(1, min(end_hour, 24))


def schedule_decision(municipality: Municipality, platform: Platform, *, moment: datetime | None = None) -> ScheduleDecision:
    timezone_name = getattr(municipality, "timezone", None) or "America/Fortaleza"
    timezone = _timezone(timezone_name)
    local = datetime.now(timezone) if moment is None else moment.replace(tzinfo=timezone) if moment.tzinfo is None else moment.astimezone(timezone)
    weekdays, start_hour, end_hour = _policy(municipality, platform)
    if local.weekday() in weekdays and start_hour <= local.hour < end_hour:
        return ScheduleDecision(True, "Dentro da janela configurada.", None, timezone_name, weekdays, start_hour, end_hour)
    candidate = local.replace(hour=start_hour, minute=0, second=0, microsecond=0)
    if local.weekday() not in weekdays or local >= candidate:
        candidate += timedelta(days=1)
    for _ in range(8):
        if candidate.weekday() in weekdays:
            break
        candidate += timedelta(days=1)
    reason = "Dia não permitido pela agenda do convênio." if local.weekday() not in weekdays else "Fora do horário configurado."
    return ScheduleDecision(False, reason, candidate.astimezone(UTC), timezone_name, weekdays, start_hour, end_hour)

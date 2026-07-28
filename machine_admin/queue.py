"""Operações transacionais da fila PostgreSQL."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import Select, delete, func, or_, select, update
from sqlalchemy.orm import Session

from machine_admin.models import (
    CredentialLease,
    ConsultationResult,
    Job,
    JobEvent,
    JobItem,
    PortalCredential,
)


def create_waiting_job(
    session: Session,
    *,
    municipality_slug: str,
    telegram_user_id: int | None = None,
    telegram_chat_id: int | None = None,
    requested_by_id: int | None = None,
) -> Job:
    job = Job(
        municipality_slug=municipality_slug,
        status="awaiting_dataset",
        telegram_user_id=telegram_user_id,
        telegram_chat_id=telegram_chat_id,
        requested_by_id=requested_by_id,
    )
    session.add(job)
    session.flush()
    session.add(
        JobEvent(
            job_id=job.id,
            event_type="created",
            message="Job criado e aguardando uma base.",
        )
    )
    return job


def credential_candidate_statement(
    *, municipality_slug: str, now: datetime
) -> Select[tuple[PortalCredential]]:
    leased_ids = select(CredentialLease.credential_id)
    return (
        select(PortalCredential)
        .where(
            PortalCredential.municipality_slug == municipality_slug,
            or_(
                PortalCredential.status == "active",
                (
                    (PortalCredential.status == "cooldown")
                    & (PortalCredential.cooldown_until <= now)
                ),
            ),
            PortalCredential.id.not_in(leased_ids),
        )
        .order_by(PortalCredential.failure_count, PortalCredential.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )


def job_item_claim_statement(
    *, job_id: int, now: datetime, batch_size: int
) -> Select[tuple[JobItem]]:
    return (
        select(JobItem)
        .where(
            JobItem.job_id == job_id,
            or_(
                JobItem.status == "pending",
                (JobItem.status == "leased") & (JobItem.lease_expires_at <= now),
            ),
        )
        .order_by(JobItem.id)
        .with_for_update(skip_locked=True)
        .limit(max(1, min(batch_size, 100)))
    )


def acquire_credential(
    session: Session,
    *,
    job_id: int,
    municipality_slug: str,
    worker_id: str,
    lease_seconds: int = 120,
) -> PortalCredential | None:
    now = datetime.now(UTC)
    session.execute(delete(CredentialLease).where(CredentialLease.expires_at <= now))
    credential = session.scalar(
        credential_candidate_statement(municipality_slug=municipality_slug, now=now)
    )
    if not credential:
        return None
    if credential.status == "cooldown":
        credential.status = "active"
        credential.cooldown_until = None
    session.add(
        CredentialLease(
            credential_id=credential.id,
            job_id=job_id,
            worker_id=worker_id,
            heartbeat_at=now,
            expires_at=now + timedelta(seconds=lease_seconds),
        )
    )
    session.flush()
    return credential


def claim_job_items(
    session: Session,
    *,
    job_id: int,
    credential_id: int,
    worker_id: str,
    batch_size: int = 10,
    lease_seconds: int = 120,
) -> list[JobItem]:
    now = datetime.now(UTC)
    items = list(
        session.scalars(
            job_item_claim_statement(
                job_id=job_id, now=now, batch_size=batch_size
            )
        )
    )
    expires_at = now + timedelta(seconds=lease_seconds)
    for item in items:
        item.status = "leased"
        item.credential_id = credential_id
        item.lease_owner = worker_id
        item.lease_expires_at = expires_at
        item.started_at = item.started_at or now
        item.attempts += 1
    session.flush()
    return items


def heartbeat_credential(
    session: Session, *, worker_id: str, lease_seconds: int = 120
) -> bool:
    now = datetime.now(UTC)
    cursor = session.execute(
        update(CredentialLease)
        .where(CredentialLease.worker_id == worker_id)
        .values(heartbeat_at=now, expires_at=now + timedelta(seconds=lease_seconds))
    )
    return cursor.rowcount == 1


def release_credential(session: Session, *, worker_id: str) -> None:
    session.execute(delete(CredentialLease).where(CredentialLease.worker_id == worker_id))


def refresh_job_counters(session: Session, job_id: int) -> None:
    counts = dict(
        session.execute(
            select(JobItem.status, func.count(JobItem.id))
            .where(JobItem.job_id == job_id)
            .group_by(JobItem.status)
        ).all()
    )
    job = session.get(Job, job_id)
    if not job:
        return
    job.completed_items = int(counts.get("completed", 0))
    job.failed_items = int(counts.get("failed", 0))
    if job.total_items and job.completed_items + job.failed_items >= job.total_items:
        job.status = "failed" if job.failed_items else "completed"
        job.finished_at = datetime.now(UTC)


def complete_job_item(
    session: Session,
    *,
    worker_id: str,
    item_id: int,
    status: str,
    result_ciphertext: bytes,
    error_code: str | None = None,
    error_message: str | None = None,
) -> JobItem:
    item = session.scalar(
        select(JobItem)
        .where(JobItem.id == item_id)
        .with_for_update()
    )
    if not item or item.status != "leased" or item.lease_owner != worker_id:
        raise ValueError("Item não pertence a este worker ou o lease expirou.")
    job = session.get(Job, item.job_id)
    if not job or job.status not in {"queued", "running"}:
        raise ValueError("O job foi cancelado ou não está mais executável.")
    item.status = status
    item.finished_at = datetime.now(UTC)
    item.lease_expires_at = None
    item.error_code = error_code
    item.error_message = error_message
    result = session.scalar(
        select(ConsultationResult).where(ConsultationResult.job_item_id == item.id)
    ) or ConsultationResult(job_item_id=item.id)
    result.credential_id = item.credential_id
    result.status = status
    result.result_ciphertext = result_ciphertext
    session.add(result)
    refresh_job_counters(session, item.job_id)
    session.flush()
    return item

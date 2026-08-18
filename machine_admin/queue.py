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
    JobItemAttempt,
    PortalCredential,
)


RETRYABLE_OUTCOMES = frozenset(
    {
        "retryable_error",
        "credential_error",
        "portal_unavailable",
        "integration_unavailable",
    }
)
SUCCESS_OUTCOMES = frozenset({"found", "not_found"})

_OUTCOME_COUNTERS = {
    "found": "found_items",
    "not_found": "not_found_items",
    "permanent_error": "permanent_items",
    **{outcome: "retryable_items" for outcome in RETRYABLE_OUTCOMES},
}


def _change_counter(job: Job, attribute: str, delta: int) -> None:
    """Aplica um delta a um contador já protegido pelo lock do ``Job``.

    O ``max`` mantém compatibilidade com jobs históricos que possam ter sido
    criados antes dos contadores por outcome. Em um job consistente o valor
    nunca é truncado: toda remoção corresponde a um estado previamente
    contabilizado.
    """
    current = int(getattr(job, attribute, 0) or 0)
    setattr(job, attribute, max(0, current + delta))


def _apply_job_counter_delta(
    job: Job,
    *,
    old_status: str,
    old_outcome: str | None,
    new_status: str,
    new_outcome: str | None,
) -> None:
    """Atualiza os agregados do job a partir de uma única transição de item.

    O chamador deve manter ``SELECT ... FOR UPDATE`` na linha do job. Isso
    transforma a atualização em O(1), sem recontar todos os CPFs a cada
    conclusão, e serializa os deltas de workers concorrentes.
    """
    status_counters = {
        "completed": "completed_items",
        "failed": "failed_items",
    }
    old_status_counter = status_counters.get(old_status)
    new_status_counter = status_counters.get(new_status)
    if old_status_counter != new_status_counter:
        if old_status_counter:
            _change_counter(job, old_status_counter, -1)
        if new_status_counter:
            _change_counter(job, new_status_counter, 1)

    old_outcome_counter = _OUTCOME_COUNTERS.get(old_outcome)
    new_outcome_counter = _OUTCOME_COUNTERS.get(new_outcome)
    if old_outcome_counter != new_outcome_counter:
        if old_outcome_counter:
            _change_counter(job, old_outcome_counter, -1)
        if new_outcome_counter:
            _change_counter(job, new_outcome_counter, 1)


def _finalize_job_from_counters(job: Job, *, now: datetime | None = None) -> None:
    """Fecha o job quando os deltas mostram que não há itens em aberto."""
    completed = int(job.completed_items or 0)
    failed = int(job.failed_items or 0)
    if not job.total_items or completed + failed < job.total_items:
        return
    if not failed:
        job.status = "completed"
    elif completed:
        job.status = "completed_with_errors"
    else:
        job.status = "failed"
    job.finished_at = now or datetime.now(UTC)


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
                    & or_(
                        PortalCredential.cooldown_until.is_(None),
                        PortalCredential.cooldown_until <= now,
                    )
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
            JobItem.attempts < JobItem.max_attempts,
            or_(
                (JobItem.status == "pending")
                & or_(
                    JobItem.next_attempt_at.is_(None),
                    JobItem.next_attempt_at <= now,
                ),
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
    job = session.scalar(
        select(Job).where(Job.id == job_id).with_for_update()
    )
    if not job or job.status not in {"queued", "running"}:
        raise ValueError("O job foi cancelado ou não está mais executável.")
    items = list(
        session.scalars(
            job_item_claim_statement(
                job_id=job_id, now=now, batch_size=batch_size
            )
        )
    )
    expires_at = now + timedelta(seconds=lease_seconds)
    for item in items:
        if item.status == "leased":
            session.execute(
                update(JobItemAttempt)
                .where(
                    JobItemAttempt.job_item_id == item.id,
                    JobItemAttempt.attempt_number == item.attempts,
                    JobItemAttempt.status == "started",
                )
                .values(
                    status="abandoned",
                    error_category="worker_lease_expired",
                    error_message="Lease do worker expirou antes da conclusão.",
                    finished_at=now,
                )
            )
        item.status = "leased"
        item.credential_id = credential_id
        item.lease_owner = worker_id
        item.lease_expires_at = expires_at
        item.started_at = item.started_at or now
        item.attempts += 1
        item.last_attempt_at = now
        item.next_attempt_at = None
        session.add(
            JobItemAttempt(
                job_item_id=item.id,
                attempt_number=item.attempts,
                credential_id=credential_id,
                worker_id=worker_id,
                status="started",
                started_at=now,
            )
        )
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
    session.execute(
        update(JobItem)
        .where(JobItem.lease_owner == worker_id, JobItem.status == "leased")
        .values(lease_expires_at=now + timedelta(seconds=lease_seconds))
    )
    return cursor.rowcount == 1


def release_credential(session: Session, *, worker_id: str) -> None:
    session.execute(delete(CredentialLease).where(CredentialLease.worker_id == worker_id))


def expire_exhausted_job_items(
    session: Session, *, job_id: int, now: datetime | None = None
) -> int:
    """Finaliza tentativas cujo worker caiu já no último uso permitido.

    A reconciliação é chamada antes de anunciar trabalho ao pool. Assim, um
    lease expirado na última tentativa não fica eternamente como ``running`` e
    também não é entregue uma quarta vez ao portal.
    """
    moment = now or datetime.now(UTC)
    job = session.scalar(
        select(Job).where(Job.id == job_id).with_for_update()
    )
    if not job or job.status not in {"queued", "running"}:
        return 0
    items = list(
        session.scalars(
            select(JobItem)
            .where(
                JobItem.job_id == job_id,
                JobItem.attempts >= JobItem.max_attempts,
                or_(
                    JobItem.status == "pending",
                    (
                        (JobItem.status == "leased")
                        & (JobItem.lease_expires_at <= moment)
                    ),
                ),
            )
            .with_for_update(skip_locked=True)
        )
    )
    if not items:
        return 0
    item_ids: list[int] = []
    for item in items:
        old_status = item.status
        old_outcome = item.outcome
        if item.status == "leased":
            session.execute(
                update(JobItemAttempt)
                .where(
                    JobItemAttempt.job_item_id == item.id,
                    JobItemAttempt.attempt_number == item.attempts,
                    JobItemAttempt.status == "started",
                )
                .values(
                    status="abandoned",
                    error_category="worker_lease_expired",
                    error_message=(
                        "Lease expirou na última tentativa permitida."
                    ),
                    finished_at=moment,
                )
            )
        item.status = "failed"
        if item.outcome not in RETRYABLE_OUTCOMES:
            item.outcome = "retryable_error"
        item.last_error_category = item.outcome
        item.error_code = item.error_code or "attempt_limit_after_worker_exit"
        item.error_message = item.error_message or (
            "Worker encerrou antes da resposta na última tentativa permitida."
        )
        item.credential_id = None
        item.lease_owner = None
        item.lease_expires_at = None
        item.next_attempt_at = None
        item.finished_at = moment
        _apply_job_counter_delta(
            job,
            old_status=old_status,
            old_outcome=old_outcome,
            new_status=item.status,
            new_outcome=item.outcome,
        )
        item_ids.append(item.id)
    session.add(
        JobEvent(
            job_id=job_id,
            event_type="consulta.limite_esgotado",
            message=(
                f"{len(items)} item(ns) finalizado(s) após expiração do último lease."
            ),
            event_data={"count": len(items), "item_ids": item_ids[:50]},
        )
    )
    _finalize_job_from_counters(job, now=moment)
    session.flush()
    return len(items)


def refresh_job_counters(session: Session, job_id: int) -> None:
    """Reconcilia explicitamente os agregados a partir dos itens persistidos.

    Esta varredura O(n) é reservada a manutenção/auditoria; o hot path usa
    deltas O(1) enquanto mantém o lock da linha do job.
    """
    # Serializa o fechamento. Sem este lock, duas conclusões concorrentes
    # podem observar contagens parciais e deixar o job preso em ``running``.
    job = session.scalar(
        select(Job).where(Job.id == job_id).with_for_update()
    )
    if not job:
        return
    counts = dict(
        session.execute(
            select(JobItem.status, func.count(JobItem.id))
            .where(JobItem.job_id == job_id)
            .group_by(JobItem.status)
        ).all()
    )
    job.completed_items = int(counts.get("completed", 0))
    job.failed_items = int(counts.get("failed", 0))
    outcome_counts = dict(
        session.execute(
            select(JobItem.outcome, func.count(JobItem.id))
            .where(JobItem.job_id == job_id, JobItem.outcome.is_not(None))
            .group_by(JobItem.outcome)
        ).all()
    )
    job.found_items = int(outcome_counts.get("found", 0))
    job.not_found_items = int(outcome_counts.get("not_found", 0))
    job.retryable_items = sum(
        int(outcome_counts.get(outcome, 0)) for outcome in RETRYABLE_OUTCOMES
    )
    job.permanent_items = int(outcome_counts.get("permanent_error", 0))
    _finalize_job_from_counters(job)


def _finish_attempt(
    session: Session,
    *,
    item: JobItem,
    worker_id: str,
    outcome: str,
    error_code: str | None,
    error_message: str | None,
    duration_ms: int | None = None,
    stage: str | None = None,
    details: dict | None = None,
) -> None:
    attempt = session.scalar(
        select(JobItemAttempt).where(
            JobItemAttempt.job_item_id == item.id,
            JobItemAttempt.attempt_number == item.attempts,
            JobItemAttempt.worker_id == worker_id,
        )
    )
    now = datetime.now(UTC)
    if not attempt:
        attempt = JobItemAttempt(
            job_item_id=item.id,
            attempt_number=max(item.attempts, 1),
            credential_id=item.credential_id,
            worker_id=worker_id,
            started_at=item.last_attempt_at or now,
        )
        session.add(attempt)
    attempt.status = outcome
    attempt.error_category = outcome if outcome not in SUCCESS_OUTCOMES else None
    attempt.error_code = error_code
    attempt.error_message = error_message
    attempt.duration_ms = duration_ms
    attempt.details_json = {**(details or {}), **({"stage": stage} if stage else {})}
    attempt.finished_at = now


def _retry_delay(item: JobItem, requested_seconds: int | None = None) -> int:
    if requested_seconds is not None:
        return max(5, min(requested_seconds, 86_400))
    return min(30 * (2 ** max(item.attempts - 1, 0)), 3_600)


def _apply_retry(
    item: JobItem,
    *,
    outcome: str,
    error_code: str | None,
    error_message: str | None,
    retry_after_seconds: int | None,
) -> bool:
    """Aplica backoff; retorna ``True`` quando o limite foi esgotado."""
    now = datetime.now(UTC)
    item.outcome = outcome
    item.error_code = error_code
    item.error_message = error_message
    item.last_error_category = outcome
    item.credential_id = None
    item.lease_owner = None
    item.lease_expires_at = None
    if item.attempts >= item.max_attempts:
        item.status = "failed"
        item.finished_at = now
        item.next_attempt_at = None
        return True
    item.status = "pending"
    item.finished_at = None
    item.next_attempt_at = now + timedelta(
        seconds=_retry_delay(item, retry_after_seconds)
    )
    return False


def complete_job_item(
    session: Session,
    *,
    worker_id: str,
    item_id: int,
    status: str,
    result_ciphertext: bytes,
    outcome: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    duration_ms: int | None = None,
    stage: str | None = None,
    details: dict | None = None,
    retry_after_seconds: int | None = None,
) -> JobItem:
    job_id = session.scalar(
        select(JobItem.job_id).where(JobItem.id == item_id)
    )
    job = (
        session.scalar(
            select(Job).where(Job.id == job_id).with_for_update()
        )
        if job_id is not None
        else None
    )
    item = session.scalar(
        select(JobItem)
        .where(JobItem.id == item_id)
        .with_for_update()
    )
    if not item or item.status != "leased" or item.lease_owner != worker_id:
        raise ValueError("Item não pertence a este worker ou o lease expirou.")
    if not job or job.id != item.job_id or job.status not in {"queued", "running"}:
        raise ValueError("O job foi cancelado ou não está mais executável.")
    canonical_outcome = outcome or (
        "found" if status == "completed" else "permanent_error"
    )
    old_status = item.status
    old_outcome = item.outcome
    _finish_attempt(
        session,
        item=item,
        worker_id=worker_id,
        outcome=canonical_outcome,
        error_code=error_code,
        error_message=error_message,
        duration_ms=duration_ms,
        stage=stage,
        details=details,
    )
    if canonical_outcome in RETRYABLE_OUTCOMES:
        exhausted = _apply_retry(
            item,
            outcome=canonical_outcome,
            error_code=error_code,
            error_message=error_message,
            retry_after_seconds=retry_after_seconds,
        )
        session.add(
            JobEvent(
                job_id=item.job_id,
                event_type="consulta.erro" if exhausted else "consulta.reenfileirada",
                message=(
                    error_message
                    or (
                        "Limite de tentativas atingido."
                        if exhausted
                        else "Falha transitória; nova tentativa agendada."
                    )
                )[:500],
                event_data={
                    "item_id": item.id,
                    "outcome": canonical_outcome,
                    "attempt": item.attempts,
                    "max_attempts": item.max_attempts,
                    "next_attempt_at": (
                        item.next_attempt_at.isoformat() if item.next_attempt_at else None
                    ),
                },
            )
        )
        _apply_job_counter_delta(
            job,
            old_status=old_status,
            old_outcome=old_outcome,
            new_status=item.status,
            new_outcome=item.outcome,
        )
        _finalize_job_from_counters(job)
        session.flush()
        return item

    item.outcome = canonical_outcome
    item.status = "completed" if canonical_outcome in SUCCESS_OUTCOMES else "failed"
    item.finished_at = datetime.now(UTC)
    item.lease_expires_at = None
    item.lease_owner = None
    item.error_code = error_code
    item.error_message = error_message
    item.last_error_category = (
        canonical_outcome if canonical_outcome not in SUCCESS_OUTCOMES else None
    )
    item.next_attempt_at = None
    result = session.scalar(
        select(ConsultationResult).where(ConsultationResult.job_item_id == item.id)
    ) or ConsultationResult(job_item_id=item.id)
    result.credential_id = item.credential_id
    result.status = canonical_outcome
    result.result_ciphertext = result_ciphertext
    result.attempt_number = item.attempts
    result.superseded_at = None
    result.consulted_at = datetime.now(UTC)
    session.add(result)
    if item.status == "failed":
        session.add(
            JobEvent(
                job_id=item.job_id,
                event_type="consulta.erro",
                message=(error_message or "Falha permanente na consulta.")[:500],
                event_data={
                    "item_id": item.id,
                    "credential_id": item.credential_id,
                    "outcome": canonical_outcome,
                    "error_code": error_code,
                },
            )
        )
    else:
        session.add(
            JobEvent(
                job_id=item.job_id,
                event_type="consulta.concluida",
                message=(
                    "Consulta concluída com servidor encontrado."
                    if canonical_outcome == "found"
                    else "Consulta concluída: servidor não encontrado pelo portal."
                ),
                event_data={
                    "item_id": item.id,
                    "credential_id": item.credential_id,
                    "outcome": canonical_outcome,
                    "attempt": item.attempts,
                    "duration_ms": duration_ms,
                },
            )
        )
    _apply_job_counter_delta(
        job,
        old_status=old_status,
        old_outcome=old_outcome,
        new_status=item.status,
        new_outcome=item.outcome,
    )
    _finalize_job_from_counters(job)
    session.flush()
    return item


def requeue_job_item(
    session: Session,
    *,
    worker_id: str,
    item_id: int,
    reason: str,
    outcome: str = "retryable_error",
    error_code: str | None = None,
    stage: str | None = None,
    retry_after_seconds: int | None = None,
) -> JobItem:
    """Retorna um lease à fila sem contaminar o resultado do job."""
    job_id = session.scalar(
        select(JobItem.job_id).where(JobItem.id == item_id)
    )
    job = (
        session.scalar(
            select(Job).where(Job.id == job_id).with_for_update()
        )
        if job_id is not None
        else None
    )
    item = session.scalar(
        select(JobItem).where(JobItem.id == item_id).with_for_update()
    )
    if not item or item.status != "leased" or item.lease_owner != worker_id:
        raise ValueError("Item não pertence a este worker ou o lease expirou.")
    if not job or job.id != item.job_id or job.status not in {"queued", "running"}:
        raise ValueError("O job foi cancelado ou não está mais executável.")
    old_status = item.status
    old_outcome = item.outcome
    _finish_attempt(
        session,
        item=item,
        worker_id=worker_id,
        outcome=outcome,
        error_code=error_code,
        error_message=reason,
        stage=stage,
    )
    exhausted = _apply_retry(
        item,
        outcome=outcome,
        error_code=error_code,
        error_message=reason,
        retry_after_seconds=retry_after_seconds,
    )
    session.add(
        JobEvent(
            job_id=item.job_id,
            event_type="consulta.erro" if exhausted else "consulta.reenfileirada",
            message=reason[:500],
            event_data={
                "item_id": item.id,
                "outcome": outcome,
                "attempt": item.attempts,
                "max_attempts": item.max_attempts,
                "next_attempt_at": (
                    item.next_attempt_at.isoformat() if item.next_attempt_at else None
                ),
            },
        )
    )
    _apply_job_counter_delta(
        job,
        old_status=old_status,
        old_outcome=old_outcome,
        new_status=item.status,
        new_outcome=item.outcome,
    )
    _finalize_job_from_counters(job)
    session.flush()
    return item

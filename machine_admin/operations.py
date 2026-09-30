"""Services shared by the admin UI, public API and recurring scheduler."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from machine_admin.config import Settings
from machine_admin.datasets import create_job_for_dataset
from machine_admin.models import ConsultationResult, Dataset, DatasetRecord, Job, JobItem, JobRequest, Municipality, PortalCredential
from machine_admin.security import SecretCipher

ACTIVE_JOB_STATES = frozenset({"awaiting_dataset", "queued", "running", "pausing", "paused", "cancelling", "blocked"})
TERMINAL_JOB_STATES = frozenset({"completed", "completed_with_errors", "cancelled", "failed"})


def validate_execution_selection(session: Session, *, dataset_id: int, selected_credential_ids: list[int] | None, max_parallel_accounts: int, require_usable: bool = True) -> tuple[Dataset, list[int]]:
    dataset = session.get(Dataset, dataset_id)
    if not dataset or dataset.status != "ready" or dataset.row_count <= 0:
        raise ValueError("Selecione uma base importada e pronta, com registros válidos.")
    municipality = session.get(Municipality, dataset.municipality_slug)
    if not municipality or not municipality.enabled or municipality.operational_status not in {"ready", "degraded"}:
        raise ValueError("O convênio precisa estar habilitado e homologado para consultas.")
    if not 1 <= max_parallel_accounts <= 20:
        raise ValueError("Selecione entre 1 e 20 acessos paralelos.")
    if max_parallel_accounts > municipality.max_workers:
        raise ValueError(f"O convênio permite até {municipality.max_workers} acesso(s) paralelo(s). Ajuste o limite nas configurações do convênio antes de selecionar mais acessos.")
    if selected_credential_ids is not None and (not selected_credential_ids or any(isinstance(i, bool) or i <= 0 for i in selected_credential_ids)):
        raise ValueError("Selecione pelo menos um acesso válido.")
    requested = sorted(set(selected_credential_ids or []))
    statement = select(PortalCredential).where(PortalCredential.municipality_slug == dataset.municipality_slug)
    if selected_credential_ids is not None:
        statement = statement.where(PortalCredential.id.in_(requested))
    accounts = list(session.scalars(statement.order_by(PortalCredential.id)))
    if requested and {a.id for a in accounts} != set(requested):
        raise ValueError("Um dos acessos selecionados não pertence ao convênio da base.")
    now = datetime.now(UTC)
    eligible = [a for a in accounts if a.status == "active" or (a.status == "cooldown" and (a.cooldown_until is None or a.cooldown_until <= now))]
    candidates = eligible if require_usable or selected_credential_ids is None else accounts
    identities: set[str] = set()
    unique = []
    for account in candidates:
        identity = (account.login_identity or account.portal_username or "").strip().lower()
        if not identity or identity in identities:
            continue
        identities.add(identity)
        unique.append(account.id)
    if require_usable and requested and set(unique) != set(requested):
        raise ValueError("Há acessos indisponíveis ou repetidos na seleção. Corrija-os ou selecione os acessos saudáveis.")
    if len(unique) < max_parallel_accounts:
        raise ValueError(f"Foram solicitados {max_parallel_accounts} acessos paralelos, mas há {len(unique)} login(s) distinto(s) disponível(is).")
    return dataset, unique


def create_execution(session: Session, *, dataset_id: int, requested_by_id: int | None, selected_credential_ids: list[int] | None = None, max_parallel_accounts: int = 1, idempotency_key: str | None = None, idempotency_namespace: str | None = None) -> Job:
    payload = {"dataset_id": dataset_id, "selected_credential_ids": sorted(set(selected_credential_ids)) if selected_credential_ids is not None else None, "max_parallel_accounts": max_parallel_accounts}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    namespace = idempotency_namespace or f"user:{requested_by_id}"
    if idempotency_key:
        if len(idempotency_key) > 160 or len(namespace) > 120 or not idempotency_key.strip():
            raise ValueError("Chave de idempotência inválida (máximo 160 caracteres).")
        # Key lock precedes creating a job: concurrent retries cannot create orphan jobs.
        lock = int.from_bytes(hashlib.sha256(f"{namespace}:{idempotency_key}".encode()).digest()[:8], "big", signed=True)
        session.execute(select(func.pg_advisory_xact_lock(lock)))
        previous = session.get(JobRequest, (namespace, idempotency_key))
        if previous:
            if previous.payload_hash != digest:
                raise ValueError("A chave de idempotência já foi usada com outra solicitação.")
            return session.get(Job, previous.job_id)
    dataset, accounts = validate_execution_selection(session, **payload)
    job = create_job_for_dataset(session, dataset=dataset, requested_by_id=requested_by_id)
    # Freeze the account selection so adding a new account tomorrow cannot change this execution.
    job.selected_credential_ids = accounts
    job.max_parallel_accounts = max_parallel_accounts
    job.result_version = 1
    if idempotency_key:
        session.add(JobRequest(namespace=namespace, request_key=idempotency_key, payload_hash=digest, job_id=job.id))
    session.flush()
    return job


def serialize_job(job: Job) -> dict[str, Any]:
    fields = ("id", "municipality_slug", "dataset_id", "status", "selected_credential_ids", "max_parallel_accounts", "result_version", "total_items", "completed_items", "failed_items", "found_items", "not_found_items", "retryable_items", "permanent_items", "error_message")
    data = {field: getattr(job, field, None) for field in fields}
    for field in ("created_at", "updated_at", "started_at", "finished_at", "not_before"):
        moment = getattr(job, field, None)
        data[field] = moment.isoformat() if moment else None
    processed = int(job.completed_items or 0) + int(job.failed_items or 0)
    data["pending_items"] = max(0, int(job.total_items or 0) - processed)
    data["progress_percent"] = min(100, round(100 * processed / job.total_items, 1)) if job.total_items else 0
    return data


def result_page(session: Session, settings: Settings, job_id: int, *, after_id: int = 0, limit: int = 100, outcome: str | None = None) -> dict[str, Any]:
    if not 1 <= limit <= 500 or after_id < 0:
        raise ValueError("Paginação inválida; limite entre 1 e 500.")
    statement = (select(JobItem, DatasetRecord, ConsultationResult)
        .join(DatasetRecord, DatasetRecord.id == JobItem.dataset_record_id)
        .outerjoin(ConsultationResult, ConsultationResult.job_item_id == JobItem.id)
        .where(JobItem.job_id == job_id, JobItem.id > after_id).order_by(JobItem.id).limit(limit + 1))
    if outcome:
        if outcome not in {"found", "not_found", "retryable_error", "permanent_error", "credential_error", "portal_unavailable", "integration_unavailable"}:
            raise ValueError("Filtro de resultado inválido.")
        statement = statement.where(JobItem.outcome == outcome)
    rows = list(session.execute(statement))
    cipher = SecretCipher(settings.master_key)
    items = []
    for item, record, result in rows[:limit]:
        valid_result = result is not None and result.superseded_at is None and (result.attempt_number is None or result.attempt_number == item.attempts)
        items.append({"id": item.id, "row_number": record.row_number,
            "cpf": cipher.decrypt(record.cpf_ciphertext, context=f"record:{record.encryption_context}:cpf"),
            "registration": record.registration, "status": item.status, "outcome": item.outcome,
            "attempts": item.attempts, "credential_id": item.credential_id,
            "error_code": item.error_code, "error_message": item.error_message,
            "consulted_at": result.consulted_at.isoformat() if valid_result and result.consulted_at else None,
            "result": json.loads(cipher.decrypt(result.result_ciphertext, context=f"result:{item.id}")) if valid_result else None})
    return {"job_id": job_id, "items": items, "count": len(items), "next_cursor": items[-1]["id"] if len(rows) > limit else None}

"""Motor transacional comum a todos os adapters de portal."""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from services.execution import ExecutionOutcome, OutcomeKind
from services.utils import mask_cpf
from workers.api_client import WorkerAPIClient, WorkerAPIConflict, WorkerAPIError


LOG = logging.getLogger(__name__)


class AdapterError(RuntimeError):
    """Erro conhecido do adapter já classificado para o motor."""

    def __init__(
        self,
        kind: OutcomeKind,
        message: str,
        *,
        code: str | None = None,
        retry_after_seconds: int | None = None,
        end_session: bool = True,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.code = code
        self.retry_after_seconds = retry_after_seconds
        self.end_session = end_session

    def to_outcome(
        self, *, stage: str, item: "WorkItem | None" = None
    ) -> ExecutionOutcome:
        return ExecutionOutcome.error(
            self.kind,
            requested=item.requested if item else {},
            code=self.code or type(self).__name__,
            message=str(self)[:500],
            stage=stage,
            retry_after_seconds=self.retry_after_seconds,
            end_session=self.end_session,
        )


@dataclass(frozen=True, slots=True)
class CredentialPayload:
    credential_id: int
    username: str
    password: str
    login_url: str | None
    query_url: str | None
    settings: dict[str, Any]

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> "CredentialPayload":
        settings = payload.get("settings")
        return cls(
            credential_id=int(payload["credential_id"]),
            username=str(payload["username"]),
            password=str(payload["password"]),
            login_url=str(payload["login_url"]) if payload.get("login_url") else None,
            query_url=str(payload["query_url"]) if payload.get("query_url") else None,
            settings=dict(settings) if isinstance(settings, dict) else {},
        )


@dataclass(frozen=True, slots=True)
class WorkItem:
    item_id: int
    cpf: str
    registration: str | None

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> "WorkItem":
        registration = payload.get("registration")
        return cls(
            item_id=int(payload["item_id"]),
            cpf=str(payload["cpf"]),
            registration=str(registration).strip() if registration is not None else None,
        )

    @property
    def requested(self) -> dict[str, str | None]:
        return {"cpf": self.cpf, "registration": self.registration}


@runtime_checkable
class PortalSession(Protocol):
    def consult(self, item: WorkItem) -> ExecutionOutcome: ...

    def close(self) -> None: ...


@runtime_checkable
class PortalAdapter(Protocol):
    platform: str
    batch_size: int
    lease_seconds: int

    def open_session(self, credential: CredentialPayload) -> PortalSession: ...

    def classify_exception(
        self,
        exc: Exception,
        *,
        stage: str,
        item: WorkItem | None = None,
    ) -> ExecutionOutcome: ...


class GenericWorker:
    """Orquestra fila/leases; não conhece seletores nem regras de portal."""

    def __init__(
        self,
        api: WorkerAPIClient,
        worker_id: str,
        stop_event: threading.Event,
        adapter: PortalAdapter,
        *,
        poll_seconds: int = 10,
    ) -> None:
        self.api = api
        self.worker_id = worker_id
        self.stop_event = stop_event
        self.adapter = adapter
        self.poll_seconds = poll_seconds
        self._current_job_id: int | None = None
        self._current_municipality: str | None = None
        self._current_credential_id: int | None = None

    def run_forever(self) -> None:
        LOG.info("Worker %s iniciado para %s.", self.worker_id, self.adapter.platform)
        self._report_worker_status("idle")
        while not self.stop_event.is_set():
            try:
                worked = self.run_once()
            except Exception as exc:
                # Uma falha inesperada não pode matar silenciosamente apenas uma
                # thread e deixar o supervisor acreditando que o pool está vivo.
                LOG.exception("Falha inesperada no worker %s.", self.worker_id)
                self._report_worker_status(
                    "backoff", health_status="degraded", last_error=str(exc)[:1000]
                )
                # Preserve o diagnóstico durante o backoff. Um novo polling
                # bem-sucedido limpará o estado; não anuncie healthy no mesmo
                # instante em que a falha acabou de ocorrer.
                self.stop_event.wait(self.poll_seconds)
                continue
            if not worked:
                self._report_worker_status("idle")
                self.stop_event.wait(self.poll_seconds)
        self._report_worker_status("stopped", health_status="stopping")

    def run_once(self) -> bool:
        status = self.api.request("GET", "/api/jobs/status")
        candidates = [
            job
            for key in ("running", "queued")
            for job in status.get(key, [])
            if job.get("platform") == self.adapter.platform
            and job.get("status") != "awaiting_dataset"
            and job.get("executable") is True
        ]
        for job in candidates:
            if self.stop_event.is_set():
                break
            if self._process_job(int(job["id"]), str(job["prefeitura"])):
                return True
        return False

    def _process_job(self, job_id: int, municipality_slug: str) -> bool:
        self._current_job_id = job_id
        self._current_municipality = municipality_slug
        self._report_worker_status("starting")
        try:
            raw_credential = self.api.request(
                "POST",
                "/api/workers/credentials/acquire",
                json={
                    "job_id": job_id,
                    "municipality_slug": municipality_slug,
                    "worker_id": self.worker_id,
                    "lease_seconds": self.adapter.lease_seconds,
                },
            )
        except WorkerAPIConflict:
            self._clear_assignment()
            self._report_worker_status("idle")
            return False
        except Exception:
            self._clear_assignment()
            raise

        credential = CredentialPayload.from_api(raw_credential)
        self._current_credential_id = credential.credential_id
        session: PortalSession | None = None
        try:
            try:
                session = self.adapter.open_session(credential)
            except Exception as exc:
                outcome = self.adapter.classify_exception(exc, stage="login")
                self._report_session_failure(credential.credential_id, outcome)
                return False

            self._report_credential(credential.credential_id, "success")
            self._report_worker_status("busy")
            return self._consume_job(job_id, credential.credential_id, session)
        finally:
            if session is not None:
                try:
                    session.close()
                except Exception:
                    LOG.exception("Falha ao fechar sessão %s.", self.worker_id)
            try:
                self.api.request(
                    "POST",
                    "/api/workers/release",
                    json={
                        "worker_id": self.worker_id,
                        "lease_seconds": self.adapter.lease_seconds,
                    },
                )
            except WorkerAPIError:
                pass
            self._clear_assignment()
            self._report_worker_status("idle")

    def _consume_job(
        self, job_id: int, credential_id: int, session: PortalSession
    ) -> bool:
        processed = False
        while not self.stop_event.is_set():
            self._heartbeat()
            try:
                claimed = self.api.request(
                    "POST",
                    "/api/workers/items/claim",
                    json={
                        "job_id": job_id,
                        "credential_id": credential_id,
                        "worker_id": self.worker_id,
                        "batch_size": self.adapter.batch_size,
                        "lease_seconds": self.adapter.lease_seconds,
                    },
                )
            except WorkerAPIConflict:
                break
            items = [WorkItem.from_api(item) for item in claimed.get("items", [])]
            if not items:
                break
            for position, item in enumerate(items):
                if self.stop_event.is_set():
                    self._requeue_many(items[position:], "Worker interrompido.")
                    return processed
                LOG.info(
                    "Worker %s consultando %s (item %s).",
                    self.worker_id,
                    mask_cpf(item.cpf),
                    item.item_id,
                )
                self._heartbeat()
                started_at = time.monotonic()
                try:
                    outcome = session.consult(item)
                except Exception as exc:
                    outcome = self.adapter.classify_exception(
                        exc, stage="consultation", item=item
                    )
                action = self._apply_outcome(
                    item,
                    credential_id,
                    outcome,
                    duration_ms=int((time.monotonic() - started_at) * 1000),
                )
                processed = processed or action != "conflict"
                if action in {"conflict", "end_session"}:
                    if position + 1 < len(items):
                        self._requeue_many(
                            items[position + 1 :], "Sessão encerrada antes da consulta."
                        )
                    return processed
        return processed

    def _apply_outcome(
        self,
        item: WorkItem,
        credential_id: int,
        outcome: ExecutionOutcome,
        *,
        duration_ms: int | None = None,
    ) -> str:
        if outcome.kind in {OutcomeKind.FOUND, OutcomeKind.NOT_FOUND}:
            return self._complete(
                item, outcome, status="completed", duration_ms=duration_ms
            )

        if outcome.kind == OutcomeKind.PERMANENT_ERROR:
            return self._complete(item, outcome, status="failed", duration_ms=duration_ms)

        if outcome.kind == OutcomeKind.RETRYABLE_ERROR:
            if not self._requeue(
                item,
                outcome.message or "Falha transitória; nova tentativa.",
                outcome=outcome.kind,
                error_code=outcome.error_code,
                stage=outcome.stage,
                retry_after_seconds=outcome.retry_after_seconds,
            ):
                return "conflict"
            return "end_session" if outcome.end_session else "requeued"

        if not self._requeue(
            item,
            outcome.message or outcome.kind.value,
            outcome=outcome.kind,
            error_code=outcome.error_code,
            stage=outcome.stage,
            retry_after_seconds=outcome.retry_after_seconds,
        ):
            return "conflict"
        self._report_session_failure(credential_id, outcome)
        return "end_session"

    def _complete(
        self,
        item: WorkItem,
        outcome: ExecutionOutcome,
        *,
        status: str,
        duration_ms: int | None = None,
    ) -> str:
        try:
            self.api.request(
                "POST",
                "/api/workers/items/complete",
                json={
                    "worker_id": self.worker_id,
                    "item_id": item.item_id,
                    "status": status,
                    "outcome": outcome.kind.value,
                    "result_data": outcome.to_payload(),
                    "error_code": (
                        None
                        if status == "completed"
                        else (outcome.error_code or outcome.kind.value)[:80]
                    ),
                    "error_message": outcome.message,
                    "stage": outcome.stage,
                    "duration_ms": duration_ms,
                },
            )
        except WorkerAPIConflict:
            return "conflict"
        return "completed"

    def _heartbeat(self) -> None:
        self.api.request(
            "POST",
            "/api/workers/heartbeat",
            json={
                "worker_id": self.worker_id,
                "lease_seconds": self.adapter.lease_seconds,
            },
        )
        self._report_worker_status("busy")

    def _report_worker_status(
        self,
        activity_status: str,
        *,
        health_status: str = "healthy",
        last_error: str | None = None,
    ) -> None:
        try:
            self.api.request(
                "POST",
                "/api/workers/status",
                json={
                    "worker_id": self.worker_id,
                    "platform_slug": self.adapter.platform,
                    "municipality_slug": self._current_municipality,
                    "job_id": self._current_job_id,
                    "credential_id": self._current_credential_id,
                    "health_status": health_status,
                    "activity_status": activity_status,
                    "adapter_version": getattr(self.adapter, "version", None),
                    "hostname": socket.gethostname(),
                    "process_id": os.getpid(),
                    "last_error": last_error,
                    "ttl_seconds": max(30, min(self.poll_seconds * 4, 600)),
                    "details": {},
                },
            )
        except WorkerAPIError:
            # Compatibilidade durante rolling deploy com backends anteriores.
            pass

    def _clear_assignment(self) -> None:
        self._current_job_id = None
        self._current_municipality = None
        self._current_credential_id = None

    def _requeue(
        self,
        item: WorkItem,
        reason: str,
        *,
        outcome: OutcomeKind = OutcomeKind.RETRYABLE_ERROR,
        error_code: str | None = None,
        stage: str | None = None,
        retry_after_seconds: int | None = None,
    ) -> bool:
        try:
            self.api.request(
                "POST",
                "/api/workers/items/requeue",
                json={
                    "worker_id": self.worker_id,
                    "item_id": item.item_id,
                    "reason": reason[:500],
                    "outcome": outcome.value,
                    "error_code": error_code,
                    "stage": stage,
                    "retry_after_seconds": retry_after_seconds,
                },
            )
            return True
        except WorkerAPIConflict:
            return False

    def _requeue_many(self, items: list[WorkItem], reason: str) -> None:
        for item in items:
            if not self._requeue(item, reason):
                break

    def _report_session_failure(
        self, credential_id: int, outcome: ExecutionOutcome
    ) -> None:
        report_outcome = {
            OutcomeKind.CREDENTIAL_ERROR: "invalid_credentials",
            OutcomeKind.PORTAL_UNAVAILABLE: "portal_unavailable",
            OutcomeKind.INTEGRATION_UNAVAILABLE: "portal_unavailable",
        }.get(outcome.kind, "transient_failure")
        self._report_credential(
            credential_id,
            report_outcome,
            outcome.message or outcome.kind.value,
            cooldown_seconds=outcome.retry_after_seconds or 900,
        )

    def _report_credential(
        self,
        credential_id: int,
        outcome: str,
        error_message: str | None = None,
        *,
        cooldown_seconds: int = 900,
    ) -> None:
        self.api.request(
            "POST",
            "/api/workers/credentials/report",
            json={
                "worker_id": self.worker_id,
                "credential_id": credential_id,
                "outcome": outcome,
                "error_message": error_message,
                "cooldown_seconds": max(60, min(cooldown_seconds, 86_400)),
            },
        )

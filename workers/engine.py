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
    lease_token: str = ""

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
            lease_token=str(payload.get("lease_token") or ""),
        )


@dataclass(frozen=True, slots=True)
class WorkItem:
    item_id: int
    cpf: str
    registration: str | None
    lease_token: str = ""

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> "WorkItem":
        registration = payload.get("registration")
        return cls(
            item_id=int(payload["item_id"]),
            cpf=str(payload["cpf"]),
            registration=str(registration).strip() if registration is not None else None,
            lease_token=str(payload.get("lease_token") or ""),
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
        self._credential_lease_token: str | None = None
        self._drain_event = threading.Event()
        self._lease_lost = threading.Event()
        self._renewal_stop = threading.Event()
        self._renewal_thread: threading.Thread | None = None
        self._activity_status = "idle"
        self._last_renewal = time.monotonic()
        self._heartbeat_api = api.fork() if hasattr(api, "fork") else api

    @property
    def available_to_drain(self) -> bool:
        return self._current_credential_id is None or self._activity_status == "draining"

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
        check = self.api.request("POST", "/api/workers/access-checks/claim", json={
            "worker_id": self.worker_id, "platform_slug": self.adapter.platform,
            "lease_seconds": self.adapter.lease_seconds,
        })
        if check.get("check_id"):
            return self._process_access_check(check)
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

    def _process_access_check(self, check: dict[str, Any]) -> bool:
        credential = CredentialPayload.from_api(check["credential"])
        self._current_credential_id = credential.credential_id
        self._current_municipality = check.get("municipality_slug")
        self._credential_lease_token = credential.lease_token
        self._drain_event.clear()
        self._lease_lost.clear()
        self._activity_status = "starting"
        self._start_renewal()
        session: PortalSession | None = None
        outcome: ExecutionOutcome | None = None
        try:
            if self._should_drain():
                return False
            try:
                session = self.adapter.open_session(credential)
            except Exception as exc:
                outcome = self.adapter.classify_exception(exc, stage="login")
            if not self._lease_lost.is_set():
                self._durable_request("POST", f"/api/workers/access-checks/{check['check_id']}/complete", json={
                    "worker_id": self.worker_id,
                    "credential_lease_token": credential.lease_token,
                    "outcome": self._credential_outcome(outcome) if outcome else "success",
                    "error_code": outcome.error_code if outcome else None,
                    "message": outcome.message if outcome else "Login confirmado pelo portal.",
                })
            return True
        finally:
            self._close_and_release(session)

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
        self._credential_lease_token = credential.lease_token
        self._drain_event.clear()
        self._lease_lost.clear()
        self._activity_status = "starting"
        self._start_renewal()
        session: PortalSession | None = None
        try:
            if self._should_drain():
                return False
            try:
                session = self.adapter.open_session(credential)
            except Exception as exc:
                outcome = self.adapter.classify_exception(exc, stage="login")
                self._report_session_failure(credential.credential_id, outcome)
                return False

            if self._should_drain():
                return False
            self._report_credential(credential.credential_id, "success")
            self._activity_status = "busy"
            self._report_worker_status("busy")
            return self._consume_job(job_id, credential.credential_id, session)
        finally:
            self._close_and_release(session)

    def _close_and_release(self, session: PortalSession | None) -> None:
        self._activity_status = "draining"
        self._report_worker_status("draining")
        closed = session is None
        if session is not None:
            try:
                session.close()
                closed = True
            except Exception:
                LOG.exception("Falha ao fechar sessão %s; reserva permanece até TTL.", self.worker_id)
        # Keep renewing during logout/close; only the closed browser may
        # release the account for another worker.
        self._stop_renewal()
        if closed:
            try:
                self.api.request("POST", "/api/workers/release", json={
                    "worker_id": self.worker_id,
                    "credential_lease_token": self._credential_lease_token,
                    "lease_seconds": self.adapter.lease_seconds,
                })
            except WorkerAPIError:
                pass
        self._clear_assignment()
        self._report_worker_status("idle")

    def _consume_job(
        self, job_id: int, credential_id: int, session: PortalSession
    ) -> bool:
        processed = False
        while not self._should_drain():
            self._heartbeat()
            if self._should_drain():
                break
            try:
                claimed = self.api.request(
                    "POST",
                    "/api/workers/items/claim",
                    json={
                        "job_id": job_id,
                        "credential_id": credential_id,
                        "credential_lease_token": self._credential_lease_token,
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
                if self._should_drain():
                    self._requeue_many(items[position:], "Worker interrompido.")
                    return processed
                LOG.info(
                    "Worker %s consultando %s (item %s).",
                    self.worker_id,
                    mask_cpf(item.cpf),
                    item.item_id,
                )
                self._heartbeat()
                if self._should_drain():
                    self._requeue_many(items[position:], "Execução em encerramento.")
                    return processed
                started_at = time.monotonic()
                try:
                    outcome = session.consult(item)
                except Exception as exc:
                    outcome = self.adapter.classify_exception(
                        exc, stage="consultation", item=item
                    )
                if self._lease_lost.is_set():
                    # The server is the authority for ownership. Do not apply a
                    # result produced after loss of our lease generation.
                    return processed
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
                if outcome.kind == OutcomeKind.RETRYABLE_ERROR:
                    recover = getattr(session, "recover", None)
                    if callable(recover):
                        try:
                            recover()
                        except Exception as exc:
                            recovery = self.adapter.classify_exception(
                                exc, stage="recovery", item=item
                            )
                            self._report_session_failure(credential_id, recovery)
                            self._requeue_many(items[position + 1:], "Falha ao recuperar a página.")
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
            if outcome.end_session:
                self._report_session_failure(credential_id, outcome)
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
            self._durable_request(
                "POST",
                "/api/workers/items/complete",
                json={
                    "worker_id": self.worker_id,
                    "item_id": item.item_id,
                    "lease_token": item.lease_token,
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

    def _heartbeat(self, *, background: bool = False) -> None:
        api = self._heartbeat_api if background else self.api
        response = api.request(
            "POST",
            "/api/workers/heartbeat",
            timeout=min(10, max(3, self.adapter.lease_seconds / 4)),
            json={
                "worker_id": self.worker_id,
                "credential_lease_token": self._credential_lease_token,
                "lease_seconds": self.adapter.lease_seconds,
            },
        )
        self._last_renewal = time.monotonic()
        if response.get("drain_requested"):
            self._drain_event.set()
        activity = "draining" if self._should_drain() else self._activity_status
        self._report_worker_status(activity, api=api)

    def _should_drain(self) -> bool:
        return self.stop_event.is_set() or self._drain_event.is_set() or self._lease_lost.is_set()

    def _start_renewal(self) -> None:
        self._renewal_stop.clear()
        self._last_renewal = time.monotonic()
        self._renewal_thread = threading.Thread(
            target=self._renewal_loop, name=f"{self.worker_id}-heartbeat", daemon=True
        )
        self._renewal_thread.start()

    def _renewal_loop(self) -> None:
        # The portal may spend minutes waiting for captcha. Renew independently
        # so neither the account nor in-flight record is assigned twice.
        interval = min(15.0, max(0.05, self.adapter.lease_seconds / 4))
        while not self._renewal_stop.wait(interval):
            try:
                self._heartbeat(background=True)
            except WorkerAPIConflict:
                self._lease_lost.set()
                return
            except WorkerAPIError:
                if time.monotonic() - self._last_renewal >= self.adapter.lease_seconds:
                    self._lease_lost.set()
                    return
                LOG.warning("Renovação temporariamente indisponível para %s.", self.worker_id)

    def _stop_renewal(self) -> None:
        self._renewal_stop.set()
        if self._renewal_thread is not None:
            self._renewal_thread.join(timeout=40)
            self._renewal_thread = None

    def _durable_request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        """Repeat acknowledgements with the same lease token, never the portal query."""
        for attempt in range(4):
            try:
                return self.api.request(method, path, **kwargs)
            except WorkerAPIConflict:
                raise
            except WorkerAPIError:
                if attempt == 3:
                    raise
                self._renewal_stop.wait(min(2 ** attempt, 4))
        raise AssertionError("unreachable")

    def _report_worker_status(
        self,
        activity_status: str,
        *,
        health_status: str = "healthy",
        last_error: str | None = None,
        api: WorkerAPIClient | None = None,
    ) -> None:
        try:
            (api or self.api).request(
                "POST",
                "/api/workers/status",
                timeout=5,
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
        self._credential_lease_token = None
        self._activity_status = "idle"

    def _requeue(
        self,
        item: WorkItem,
        reason: str,
        *,
        outcome: OutcomeKind = OutcomeKind.RETRYABLE_ERROR,
        error_code: str | None = None,
        stage: str | None = None,
        retry_after_seconds: int | None = None,
        consume_attempt: bool = True,
    ) -> bool:
        try:
            self._durable_request(
                "POST",
                "/api/workers/items/requeue",
                json={
                    "worker_id": self.worker_id,
                    "item_id": item.item_id,
                    "lease_token": item.lease_token,
                    "consume_attempt": consume_attempt,
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
            if not self._requeue(item, reason, consume_attempt=False):
                break

    def _report_session_failure(
        self, credential_id: int, outcome: ExecutionOutcome
    ) -> None:
        report_outcome = self._credential_outcome(outcome)
        self._report_credential(
            credential_id,
            report_outcome,
            outcome.message or outcome.kind.value,
            cooldown_seconds=outcome.retry_after_seconds or 900,
            stage=outcome.stage,
            error_code=outcome.error_code,
        )

    @staticmethod
    def _credential_outcome(outcome: ExecutionOutcome) -> str:
        return {
            OutcomeKind.CREDENTIAL_ERROR: "invalid_credentials",
            OutcomeKind.PORTAL_UNAVAILABLE: "portal_unavailable",
            OutcomeKind.INTEGRATION_UNAVAILABLE: "integration_unavailable",
        }.get(outcome.kind, "transient_failure")

    def _report_credential(
        self,
        credential_id: int,
        outcome: str,
        error_message: str | None = None,
        *,
        cooldown_seconds: int = 900,
        stage: str | None = None,
        error_code: str | None = None,
    ) -> None:
        self.api.request(
            "POST",
            "/api/workers/credentials/report",
            json={
                "worker_id": self.worker_id,
                "credential_id": credential_id,
                "credential_lease_token": self._credential_lease_token,
                "outcome": outcome,
                "stage": stage,
                "error_code": error_code,
                "error_message": error_message,
                "cooldown_seconds": max(60, min(cooldown_seconds, 86_400)),
            },
        )

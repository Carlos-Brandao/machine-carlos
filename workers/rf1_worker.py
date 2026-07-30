"""Worker RF1/Boa Vista que consome itens do PostgreSQL via API."""

from __future__ import annotations

import logging
import os
import threading
from time import sleep

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from rf1.rf1 import (
    DEFAULT_LOGIN_URL,
    DEFAULT_QUERY_URL,
    LOGIN_PATH,
    RF1Error,
    _consultar,
    _login,
    _logout,
)
from services.scheduling import is_within_window
from services.utils import mask_cpf
from workers.api_client import WorkerAPIClient, WorkerAPIConflict, WorkerAPIError


LOG = logging.getLogger(__name__)


class RF1Worker:
    def __init__(
        self,
        api: WorkerAPIClient,
        worker_id: str,
        stop_event: threading.Event,
        *,
        poll_seconds: int = 10,
    ) -> None:
        self.api = api
        self.worker_id = worker_id
        self.stop_event = stop_event
        self.poll_seconds = poll_seconds

    def run_forever(self) -> None:
        LOG.info("Worker %s iniciado para RF1.", self.worker_id)
        while not self.stop_event.is_set():
            try:
                worked = self.run_once()
            except (WorkerAPIError, OSError, PlaywrightError, RF1Error) as exc:
                LOG.warning("Worker %s aguardando API: %s", self.worker_id, exc)
                worked = False
            if not worked:
                self.stop_event.wait(self.poll_seconds)

    def run_once(self) -> bool:
        if not is_within_window("rf1"):
            return False
        status = self.api.request("GET", "/api/jobs/status")
        candidates = [
            job
            for key in ("running", "queued")
            for job in status.get(key, [])
            if job.get("platform") == "rf1" and job.get("status") != "awaiting_dataset"
        ]
        for job in candidates:
            if self.stop_event.is_set():
                break
            if self._process_job(int(job["id"]), str(job["prefeitura"])):
                return True
        return False

    def _process_job(self, job_id: int, municipality_slug: str) -> bool:
        try:
            credential = self.api.request(
                "POST",
                "/api/workers/credentials/acquire",
                json={
                    "job_id": job_id,
                    "municipality_slug": municipality_slug,
                    "worker_id": self.worker_id,
                    "lease_seconds": 600,
                },
            )
        except WorkerAPIConflict:
            return False

        credential_id = int(credential["credential_id"])
        try:
            return self._browse(job_id, credential_id, credential)
        finally:
            try:
                self.api.request(
                    "POST",
                    "/api/workers/release",
                    json={"worker_id": self.worker_id, "lease_seconds": 600},
                )
            except WorkerAPIError:
                pass

    def _browse(
        self, job_id: int, credential_id: int, credential: dict[str, object]
    ) -> bool:
        login_url = str(credential.get("login_url") or DEFAULT_LOGIN_URL)
        query_url = str(credential.get("query_url") or DEFAULT_QUERY_URL)
        settings = credential.get("settings") or {}
        consignataria = (
            str(settings.get("consignataria") or "").strip()
            if isinstance(settings, dict)
            else ""
        ) or None
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=os.getenv("HEADLESS", "true").lower() == "true"
            )
            context = browser.new_context(viewport={"width": 1280, "height": 900})
            page = context.new_page()
            try:
                logged_in = _login(
                    page,
                    login_url,
                    str(credential["username"]),
                    str(credential["password"]),
                    consignataria,
                )
                if not logged_in:
                    self._report_credential(
                        credential_id,
                        "transient_failure",
                        "Login RF1 falhou após as tentativas configuradas.",
                    )
                    return True
                self._report_credential(credential_id, "success")
                processed = False
                while not self.stop_event.is_set():
                    self.api.request(
                        "POST",
                        "/api/workers/heartbeat",
                        json={"worker_id": self.worker_id, "lease_seconds": 600},
                    )
                    try:
                        claimed = self.api.request(
                            "POST",
                            "/api/workers/items/claim",
                            json={
                                "job_id": job_id,
                                "credential_id": credential_id,
                                "worker_id": self.worker_id,
                                "batch_size": 5,
                                "lease_seconds": 600,
                            },
                        )
                    except WorkerAPIConflict:
                        break
                    items = claimed.get("items", [])
                    if not items:
                        break
                    for item in items:
                        if self.stop_event.is_set():
                            break
                        completed = self._process_item(
                            page,
                            job_id=job_id,
                            item=item,
                            login_url=login_url,
                            query_url=query_url,
                            username=str(credential["username"]),
                            password=str(credential["password"]),
                            consignataria=consignataria,
                        )
                        if not completed:
                            return processed
                        processed = True
                return processed
            finally:
                # O RF1 mantém a sessão ativa no servidor mesmo após o
                # fechamento do Chromium. Sair explicitamente evita que o
                # próximo worker seja bloqueado como "usuário já logado".
                _logout(page)
                context.close()
                browser.close()

    def _process_item(
        self,
        page,
        *,
        job_id: int,
        item: dict[str, object],
        login_url: str,
        query_url: str,
        username: str,
        password: str,
        consignataria: str | None,
    ) -> bool:
        del job_id
        item_id = int(item["item_id"])
        cpf = str(item["cpf"])
        LOG.info("Worker %s consultando %s.", self.worker_id, mask_cpf(cpf))
        status = "completed"
        result: dict[str, object]
        error_code = None
        error_message = None
        try:
            if LOGIN_PATH.lower() in page.url.lower():
                if not _login(page, login_url, username, password, consignataria):
                    raise RuntimeError("Não foi possível renovar a sessão RF1.")
            if query_url not in page.url:
                page.goto(query_url, wait_until="domcontentloaded")
            if LOGIN_PATH.lower() in page.url.lower():
                if not _login(page, login_url, username, password, consignataria):
                    raise RuntimeError("Sessão RF1 expirou.")
                page.goto(query_url, wait_until="domcontentloaded")
            page.wait_for_selector(
                "#ctl00_ctl00_ContentPlaceHolder1_ContentPlaceHolder1_btnListar"
            )
            result = _consultar(page, cpf)
        except PlaywrightTimeoutError:
            # Um postback lento, ou uma resposta que não atualizou os labels,
            # não comprova que o servidor não existe. Registrar como timeout
            # permite repetir a consulta sem contaminar o resultado final.
            status = "failed"
            result = {"Status_Robo": "Timeout"}
            error_code = "timeout"
            error_message = "RF1 não confirmou a resposta da consulta no tempo limite."
        except Exception as exc:
            status = "failed"
            result = {"Status_Robo": "Erro"}
            error_code = type(exc).__name__[:80]
            error_message = str(exc)[:500]
        try:
            self.api.request(
                "POST",
                "/api/workers/items/complete",
                json={
                    "worker_id": self.worker_id,
                    "item_id": item_id,
                    "status": status,
                    "result_data": result,
                    "error_code": error_code,
                    "error_message": error_message,
                },
            )
        except WorkerAPIConflict:
            return False
        return True

    def _report_credential(
        self, credential_id: int, outcome: str, error_message: str | None = None
    ) -> None:
        self.api.request(
            "POST",
            "/api/workers/credentials/report",
            json={
                "worker_id": self.worker_id,
                "credential_id": credential_id,
                "outcome": outcome,
                "error_message": error_message,
                "cooldown_seconds": 900,
            },
        )

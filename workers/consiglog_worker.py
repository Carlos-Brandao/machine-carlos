"""Worker de fila para consultas ConsigX/Itabuna."""

from __future__ import annotations

import os

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from consiglog.consiglog import (
    DEFAULT_LOGIN_URL,
    DEFAULT_QUERY_URL,
    ConsiglogPortalUnavailable,
    _consult,
    _login,
)
from workers.api_client import WorkerAPIConflict, WorkerAPIError
from workers.rf1_worker import LOG, RF1Worker


class ConsiglogWorker(RF1Worker):
    """Reutiliza leases PostgreSQL; só muda a navegação do portal."""

    def run_once(self) -> bool:
        status = self.api.request("GET", "/api/jobs/status")
        candidates = [
            job
            for key in ("running", "queued")
            for job in status.get(key, [])
            if job.get("platform") == "consiglog" and job.get("status") != "awaiting_dataset"
        ]
        for job in candidates:
            if self.stop_event.is_set():
                break
            if self._process_job(int(job["id"]), str(job["prefeitura"])):
                return True
        return False

    def _browse(self, job_id: int, credential_id: int, credential: dict[str, object]) -> bool:
        login_url = str(credential.get("login_url") or DEFAULT_LOGIN_URL)
        query_url = str(credential.get("query_url") or DEFAULT_QUERY_URL)
        service = str((credential.get("settings") or {}).get("servico") or "").strip() or None
        username, password = str(credential["username"]), str(credential["password"])
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=os.getenv("HEADLESS", "true").lower() == "true")
            context = browser.new_context(viewport={"width": 1280, "height": 900})
            page = context.new_page()
            try:
                _login(page, login_url, username, password)
                self._report_credential(credential_id, "success")
                processed = False
                while not self.stop_event.is_set():
                    self.api.request("POST", "/api/workers/heartbeat", json={"worker_id": self.worker_id, "lease_seconds": 600})
                    try:
                        claimed = self.api.request("POST", "/api/workers/items/claim", json={"job_id": job_id, "credential_id": credential_id, "worker_id": self.worker_id, "batch_size": 1, "lease_seconds": 600})
                    except WorkerAPIConflict:
                        break
                    items = claimed.get("items", [])
                    if not items:
                        break
                    item = items[0]
                    status, result, code, message = "completed", {}, None, None
                    try:
                        page.goto(query_url, wait_until="domcontentloaded")
                        if page.locator("#txtLogin").count() and page.locator("#txtLogin").first.is_visible():
                            _login(page, login_url, username, password)
                            page.goto(query_url, wait_until="domcontentloaded")
                        page.locator("#body_cpfTextBox").wait_for(state="visible", timeout=20_000)
                        result = _consult(page, str(item["cpf"]), service)
                    except PlaywrightTimeoutError:
                        status, result, code, message = "failed", {"Status_Robo": "Timeout"}, "timeout", "Tempo limite na consulta ConsigX."
                    except Exception as exc:
                        status, result = "failed", {"Status_Robo": "Erro"}
                        code, message = type(exc).__name__[:80], str(exc)[:500]
                    self.api.request("POST", "/api/workers/items/complete", json={"worker_id": self.worker_id, "item_id": int(item["item_id"]), "status": status, "result_data": result, "error_code": code, "error_message": message})
                    processed = True
                return processed
            except ConsiglogPortalUnavailable as exc:
                LOG.warning("Portal ConsigX indisponível para este worker: %s", exc)
                self._report_credential(credential_id, "portal_unavailable", str(exc)[:500])
                return False
            except (WorkerAPIError, Exception) as exc:
                LOG.exception("Falha no worker ConsigX: %s", exc)
                self._report_credential(credential_id, "transient_failure", str(exc)[:500])
                return False
            finally:
                try:
                    context.close()
                except PlaywrightError:
                    pass
                try:
                    browser.close()
                except PlaywrightError:
                    pass

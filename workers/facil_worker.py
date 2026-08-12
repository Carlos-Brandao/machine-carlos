"""Worker de fila para consultas FACILCONSIG."""

from __future__ import annotations

import asyncio
import os
import re

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from facil.facil import SearchFormUnavailable, _buscar, _extrair, _login
from workers.api_client import WorkerAPIConflict, WorkerAPIError
from workers.rf1_worker import LOG, RF1Worker


class FacilWorker(RF1Worker):
    def run_once(self) -> bool:
        status = self.api.request("GET", "/api/jobs/status")
        candidates = [
            job
            for key in ("running", "queued")
            for job in status.get(key, [])
            if job.get("platform") == "facil"
            and job.get("status") != "awaiting_dataset"
        ]
        for job in candidates:
            if self.stop_event.is_set():
                break
            if self._process_job(int(job["id"]), str(job["prefeitura"])):
                return True
        return False

    def _browse(
        self, job_id: int, credential_id: int, credential: dict[str, object]
    ) -> bool:
        return asyncio.run(
            self._browse_async(job_id, credential_id, credential)
        )

    async def _browse_async(
        self, job_id: int, credential_id: int, credential: dict[str, object]
    ) -> bool:
        base_url = str(credential.get("login_url") or "").rstrip("/")
        # A URL que o portal divulga costuma terminar em index_servidor.php,
        # mas as rotas internas (controlador.php) ficam na pasta do convênio.
        if base_url.lower().endswith("/index_servidor.php"):
            base_url = base_url.rsplit("/", 1)[0]
        if not base_url:
            self._report_credential(
                credential_id, "transient_failure", "URL FACILCONSIG não configurada."
            )
            return False
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=os.getenv("HEADLESS", "true").lower() == "true"
            )
            context = await browser.new_context(viewport={"width": 1280, "height": 900})
            page = await context.new_page()
            try:
                logged_in = await _login(
                    page,
                    base_url,
                    str(credential["username"]),
                    str(credential["password"]),
                    allow_manual_fallback=False,
                )
                if not logged_in:
                    self._report_credential(
                        credential_id,
                        "transient_failure",
                        "Login FACILCONSIG falhou após as tentativas configuradas.",
                    )
                    return False
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
                                "batch_size": 1,
                                "lease_seconds": 600,
                            },
                        )
                    except WorkerAPIConflict:
                        break
                    items = claimed.get("items", [])
                    if not items:
                        break
                    item = items[0]
                    result: dict[str, object] = {}
                    item_status = "completed"
                    error_code = error_message = None
                    registration = str(item.get("registration") or "").strip()
                    try:
                        found = await _buscar(
                            page, base_url, registration, str(item["cpf"])
                        )
                        if found:
                            result = await _extrair(page)
                            # Não aceite uma ficha cujo identificador não seja
                            # o mesmo registro que foi solicitado. Isso evita
                            # salvar a ficha anterior em caso de resposta lenta
                            # ou seleção incorreta do portal.
                            expected_cpf = re.sub(r"\D", "", str(item["cpf"]))
                            expected_registration = re.sub(r"\D", "", registration)
                            returned_cpf = next(
                                (
                                    re.sub(r"\D", "", str(value))
                                    for key, value in result.items()
                                    if key.strip().lower().endswith("| cpf")
                                ),
                                "",
                            )
                            if returned_cpf != expected_cpf:
                                raise ValueError("O portal retornou uma ficha sem o CPF solicitado.")
                            returned_registration = next(
                                (
                                    re.sub(r"\D", "", str(value))
                                    for key, value in result.items()
                                    if "matrícula" in key.lower() or "matricula" in key.lower()
                                ),
                                "",
                            )
                            if (
                                expected_registration
                                and returned_registration
                                and returned_registration != expected_registration
                            ):
                                raise ValueError("O portal retornou uma ficha sem a matrícula solicitada.")
                        else:
                            result = {"Status_Robo": "Não Encontrado"}
                    except PlaywrightTimeoutError as exc:
                        item_status = "failed"
                        result = {"Status_Robo": "Timeout"}
                        error_code, error_message = "timeout", str(exc)[:500]
                    except Exception as exc:
                        item_status = "failed"
                        result = {"Status_Robo": "Erro"}
                        error_code = type(exc).__name__[:80]
                        error_message = str(exc)[:500]
                    self.api.request(
                        "POST",
                        "/api/workers/items/complete",
                        json={
                            "worker_id": self.worker_id,
                            "item_id": int(item["item_id"]),
                            "status": item_status,
                            "result_data": result,
                            "error_code": error_code,
                            "error_message": error_message,
                        },
                    )
                    processed = True
                    # A rota/reload da sessão não recuperou o formulário.
                    # Fechamos este navegador uma vez e retomamos o lote no
                    # próximo ciclo, sem repetir o mesmo timeout em massa.
                    if error_code == "SearchFormUnavailable":
                        return processed
                return processed
            except WorkerAPIError as exc:
                LOG.warning("Falha no worker FACILCONSIG: %s", exc)
                return False
            finally:
                await context.close()
                await browser.close()

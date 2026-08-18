"""Adapter FACILCONSIG para o motor genérico de workers."""

from __future__ import annotations

import asyncio
import os
import re

import requests
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from facil.facil import (
    SearchFormUnavailable,
    SearchResponseUnconfirmed,
    _buscar,
    _extrair,
    check_login_success,
)
from services.captcha import CaptchaError, resolve_captcha
from services.execution import ExecutionOutcome, OutcomeKind
from workers.engine import AdapterError, CredentialPayload, WorkItem


def _digits(value: object) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _registration(value: object) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


async def _login_without_side_effects(
    page: Page, base_url: str, username: str, password: str
) -> bool:
    """Autentica sem Telegram/fallback manual e preserva erros do 2Captcha."""

    last_portal_error: Exception | None = None
    for _attempt in range(3):
        try:
            await page.goto(
                base_url.rstrip("/") + "/",
                wait_until="domcontentloaded",
                timeout=20_000,
            )
            await page.fill("#usuario", username)
            await page.fill("#senha", password)
            captcha = await resolve_captcha(page, base_url)
            await page.fill("input[name='captcha']", captcha)
            await page.click("button[type='submit']")
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=20_000)
            except PlaywrightTimeoutError:
                # Alguns controladores concluem a navegação sem emitir um novo
                # evento de carga; a confirmação positiva abaixo é a autoridade.
                pass
            await page.wait_for_timeout(1_000)
            if await check_login_success(page, base_url):
                return True
            try:
                rejection = (await page.locator("body").inner_text()).casefold()
            except PlaywrightError:
                rejection = ""
            credential_markers = (
                "usuário ou senha inválid",
                "usuario ou senha invalid",
                "usuário/senha inválid",
                "usuario/senha invalid",
                "senha incorreta",
                "acesso não autorizado",
            )
            if any(marker in rejection for marker in credential_markers):
                raise AdapterError(
                    OutcomeKind.CREDENTIAL_ERROR,
                    "Usuário ou senha recusados pelo FACILCONSIG.",
                    code="facil_invalid_credentials",
                )
        except CaptchaError:
            raise
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            last_portal_error = exc
    if last_portal_error is not None:
        raise last_portal_error
    return False


class FacilSession:
    """Mantém Playwright assíncrono vivo atrás de uma interface síncrona."""

    def __init__(self, credential: CredentialPayload) -> None:
        self.credential = credential
        self.base_url = str(credential.login_url or "").rstrip("/")
        if self.base_url.lower().endswith("/index_servidor.php"):
            self.base_url = self.base_url.rsplit("/", 1)[0]
        if not self.base_url:
            raise AdapterError(
                OutcomeKind.PORTAL_UNAVAILABLE,
                "URL FACILCONSIG não configurada.",
                code="facil_url_missing",
            )
        self.loop = asyncio.new_event_loop()
        self.playwright = self.browser = self.context = self.page = None
        try:
            self.loop.run_until_complete(self._open())
        except Exception:
            self.close()
            raise

    async def _open(self) -> None:
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=os.getenv("HEADLESS", "true").lower() == "true"
        )
        self.context = await self.browser.new_context(viewport={"width": 1280, "height": 900})
        self.page = await self.context.new_page()
        logged_in = await _login_without_side_effects(
            self.page,
            self.base_url,
            self.credential.username,
            self.credential.password,
        )
        if not logged_in:
            raise AdapterError(
                OutcomeKind.RETRYABLE_ERROR,
                "Login FACILCONSIG não foi confirmado após as tentativas configuradas.",
                code="facil_login_not_confirmed",
                retry_after_seconds=900,
            )

    def consult(self, item: WorkItem) -> ExecutionOutcome:
        return self.loop.run_until_complete(self._consult(item))

    async def _consult(self, item: WorkItem) -> ExecutionOutcome:
        assert self.page is not None
        found = await _buscar(
            self.page,
            self.base_url,
            item.registration or "",
            item.cpf,
        )
        if not found:
            return ExecutionOutcome.not_found(requested=item.requested)

        raw = await _extrair(self.page)
        expected_cpf = _digits(item.cpf)
        returned_cpf = next(
            (
                _digits(value)
                for key, value in raw.items()
                if key.strip().lower().endswith("| cpf")
            ),
            "",
        )
        if returned_cpf != expected_cpf:
            raise AdapterError(
                OutcomeKind.RETRYABLE_ERROR,
                "O FACILCONSIG retornou uma ficha sem o CPF solicitado.",
                code="facil_cpf_mismatch",
            )

        returned_registration_raw = next(
            (
                value
                for key, value in raw.items()
                if "matrícula" in key.lower() or "matricula" in key.lower()
            ),
            "",
        )
        expected_registration = _registration(item.registration)
        returned_registration = _registration(returned_registration_raw)
        if (
            expected_registration
            and returned_registration != expected_registration
        ):
            raise AdapterError(
                OutcomeKind.RETRYABLE_ERROR,
                "O FACILCONSIG retornou uma ficha sem a matrícula solicitada.",
                code="facil_registration_mismatch",
            )

        person = {
            key: value
            for key, value in raw.items()
            if not key.lower().startswith("margem |")
        }
        margins = {
            key: value for key, value in raw.items() if key.lower().startswith("margem |")
        }
        return ExecutionOutcome.found(
            requested=item.requested,
            confirmed={
                "cpf": returned_cpf,
                "registration": returned_registration_raw or None,
            },
            person=person,
            margins=margins,
            raw=raw,
        )

    async def _close_async(self) -> None:
        if self.context is not None:
            await self.context.close()
        if self.browser is not None:
            await self.browser.close()
        if self.playwright is not None:
            await self.playwright.stop()

    def close(self) -> None:
        loop = getattr(self, "loop", None)
        if loop is None or loop.is_closed():
            return
        try:
            loop.run_until_complete(self._close_async())
        except Exception:
            pass
        finally:
            loop.close()


class FacilAdapter:
    platform = "facil"
    version = "facil.v1"
    batch_size = 1
    lease_seconds = 600

    def open_session(self, credential: CredentialPayload) -> FacilSession:
        return FacilSession(credential)

    def classify_exception(
        self,
        exc: Exception,
        *,
        stage: str,
        item: WorkItem | None = None,
    ) -> ExecutionOutcome:
        requested = item.requested if item else {}
        if isinstance(exc, AdapterError):
            return exc.to_outcome(stage=stage, item=item)
        if isinstance(exc, (CaptchaError, requests.RequestException)):
            return ExecutionOutcome.error(
                OutcomeKind.INTEGRATION_UNAVAILABLE,
                requested=requested,
                code="captcha_unavailable",
                message=str(exc)[:500],
                stage=stage,
                retry_after_seconds=900,
                end_session=True,
            )
        if isinstance(exc, (SearchFormUnavailable, SearchResponseUnconfirmed)):
            return ExecutionOutcome.error(
                OutcomeKind.RETRYABLE_ERROR,
                requested=requested,
                code="facil_search_form_unavailable",
                message=str(exc)[:500],
                stage=stage,
                retry_after_seconds=60,
                end_session=True,
            )
        if isinstance(exc, PlaywrightTimeoutError):
            return ExecutionOutcome.error(
                OutcomeKind.RETRYABLE_ERROR,
                requested=requested,
                code="facil_timeout",
                message=str(exc)[:500] or "Tempo limite na consulta FACILCONSIG.",
                stage=stage,
                end_session=True,
                raw={"Status_Robo": "Timeout"},
            )
        if isinstance(exc, (PlaywrightError, OSError)):
            return ExecutionOutcome.error(
                OutcomeKind.PORTAL_UNAVAILABLE,
                requested=requested,
                code=type(exc).__name__[:80],
                message=str(exc)[:500],
                stage=stage,
                retry_after_seconds=300,
                end_session=True,
            )
        return ExecutionOutcome.error(
            OutcomeKind.RETRYABLE_ERROR,
            requested=requested,
            code="facil_unexpected_error",
            message=str(exc)[:500],
            stage=stage,
            retry_after_seconds=60,
            end_session=True,
        )

"""Adapter ConsigX/Itabuna para o motor genérico de workers."""

from __future__ import annotations

import os
import re

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from consiglog.consiglog import (
    CPF_FIELD,
    DEFAULT_LOGIN_URL,
    DEFAULT_QUERY_URL,
    ConsiglogError,
    ConsiglogPortalUnavailable,
    ConsiglogResponseUnconfirmed,
    _configured_login_profile,
    _consult,
    _login,
    _visible,
)
from machine_admin.secret_store import get_runtime_secret
from services.execution import ExecutionOutcome, OutcomeKind
from services.proxy import parse_http_proxy
from workers.engine import AdapterError, CredentialPayload, WorkItem


def _digits(value: object) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _registration(value: object) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _proxy_settings() -> dict[str, str] | None:
    raw = get_runtime_secret("CONSIGX_HTTPS_PROXY")
    if not raw:
        return None
    try:
        return parse_http_proxy(raw).playwright_settings()
    except ValueError as exc:
        raise AdapterError(
            OutcomeKind.INTEGRATION_UNAVAILABLE,
            "Proxy ConsigX inválida.",
            code="consigx_proxy_invalid",
        ) from exc


class ConsiglogSession:
    def __init__(self, credential: CredentialPayload) -> None:
        self.credential = credential
        self.login_url = credential.login_url or DEFAULT_LOGIN_URL
        self.query_url = credential.query_url or DEFAULT_QUERY_URL
        self.service = str(credential.settings.get("servico") or "").strip() or None
        self.portal_profile = _configured_login_profile(credential.settings)
        self._playwright = sync_playwright().start()
        self.browser = self._playwright.chromium.launch(
            headless=os.getenv("HEADLESS", "true").lower() == "true",
            proxy=_proxy_settings(),
        )
        self.context = self.browser.new_context(viewport={"width": 1280, "height": 900})
        self.page = self.context.new_page()
        try:
            _login(
                self.page,
                self.login_url,
                credential.username,
                credential.password,
                self.portal_profile,
            )
            self._open_query_page(allow_relogin=False)
        except Exception:
            self.close()
            raise

    def _open_query_page(self, *, allow_relogin: bool = True) -> None:
        self.page.goto(self.query_url, wait_until="domcontentloaded", timeout=60_000)
        if _visible(self.page, "#txtLogin"):
            if not allow_relogin:
                raise AdapterError(
                    OutcomeKind.CREDENTIAL_ERROR,
                    "O ConsigX não confirmou o login.",
                    code="consigx_login_not_confirmed",
                )
            _login(
                self.page,
                self.login_url,
                self.credential.username,
                self.credential.password,
                self.portal_profile,
            )
            self.page.goto(self.query_url, wait_until="domcontentloaded", timeout=60_000)
        self.page.locator(CPF_FIELD).wait_for(state="visible", timeout=20_000)

    def consult(self, item: WorkItem) -> ExecutionOutcome:
        expected_cpf = _digits(item.cpf)
        if len(expected_cpf) != 11:
            raise AdapterError(
                OutcomeKind.PERMANENT_ERROR,
                "CPF inválido na base de entrada.",
                code="invalid_cpf",
                end_session=False,
            )
        self._open_query_page()
        raw = _consult(self.page, expected_cpf, self.service)
        if raw.get("Status_Robo") == "Não Encontrado":
            return ExecutionOutcome.not_found(requested=item.requested)
        confirmed_cpf = _digits(raw.get("CPF_Confirmado"))
        expected_registration = _registration(item.registration)
        returned_registration = _registration(raw.get("Matricula"))
        if (
            expected_registration
            and returned_registration != expected_registration
        ):
            raise AdapterError(
                OutcomeKind.RETRYABLE_ERROR,
                "O ConsigX retornou matrícula diferente da solicitada.",
                code="consigx_registration_mismatch",
            )
        person = {
            key: raw.get(key)
            for key in ("Matricula", "Categoria", "Lotacao", "Situacao")
        }
        margins = {key: value for key, value in raw.items() if key.startswith("MARGEM ")}
        return ExecutionOutcome.found(
            requested=item.requested,
            confirmed={
                "cpf": confirmed_cpf,
                "registration": raw.get("Matricula") or None,
            },
            person=person,
            margins=margins,
            raw=raw,
        )

    def close(self) -> None:
        for resource_name in ("context", "browser"):
            resource = getattr(self, resource_name, None)
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    pass
        playwright = getattr(self, "_playwright", None)
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:
                pass


class ConsiglogAdapter:
    platform = "consiglog"
    version = "consiglog.v1"
    batch_size = 1
    lease_seconds = 600

    def open_session(self, credential: CredentialPayload) -> ConsiglogSession:
        return ConsiglogSession(credential)

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
        if isinstance(exc, ConsiglogPortalUnavailable):
            return ExecutionOutcome.error(
                OutcomeKind.PORTAL_UNAVAILABLE,
                requested=requested,
                code="consigx_portal_unavailable",
                message=str(exc)[:500],
                stage=stage,
                retry_after_seconds=900,
                end_session=True,
            )
        if isinstance(exc, PlaywrightTimeoutError):
            return ExecutionOutcome.error(
                OutcomeKind.RETRYABLE_ERROR,
                requested=requested,
                code="consigx_timeout",
                message="Tempo limite na consulta ConsigX.",
                stage=stage,
                end_session=True,
                raw={"Status_Robo": "Timeout"},
            )
        if isinstance(exc, ConsiglogResponseUnconfirmed):
            return ExecutionOutcome.error(
                OutcomeKind.RETRYABLE_ERROR,
                requested=requested,
                code="consigx_response_unconfirmed",
                message=str(exc)[:500],
                stage=stage,
                retry_after_seconds=60,
                end_session=True,
            )
        if isinstance(exc, ConsiglogError):
            return ExecutionOutcome.error(
                OutcomeKind.CREDENTIAL_ERROR if stage == "login" else OutcomeKind.RETRYABLE_ERROR,
                requested=requested,
                code="consigx_error",
                message=str(exc)[:500],
                stage=stage,
                end_session=True,
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
            code="consigx_unexpected_error",
            message=str(exc)[:500],
            stage=stage,
            retry_after_seconds=60,
            end_session=True,
        )

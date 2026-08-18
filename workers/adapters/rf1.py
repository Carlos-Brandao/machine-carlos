"""Adapter RF1/Boa Vista para o motor genérico de workers."""

from __future__ import annotations

import os
import re

import requests
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from rf1.rf1 import (
    DEFAULT_LOGIN_URL,
    DEFAULT_QUERY_URL,
    LOGIN_PATH,
    RF1Error,
    RF1NotFound,
    _consultar,
    _login,
    _logout,
)
from services.captcha import CaptchaError
from services.execution import ExecutionOutcome, OutcomeKind
from workers.engine import AdapterError, CredentialPayload, PortalSession, WorkItem


def _digits(value: object) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _registration(value: object) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


class RF1Session(PortalSession):
    def __init__(self, credential: CredentialPayload) -> None:
        self.credential = credential
        self.login_url = credential.login_url or DEFAULT_LOGIN_URL
        self.query_url = credential.query_url or DEFAULT_QUERY_URL
        configured = str(credential.settings.get("consignataria") or "").strip()
        self.consignataria = configured or None
        self._playwright = sync_playwright().start()
        self.browser = self._playwright.chromium.launch(
            headless=os.getenv("HEADLESS", "true").lower() == "true"
        )
        self.context = self.browser.new_context(viewport={"width": 1280, "height": 900})
        self.page = self.context.new_page()
        try:
            if not _login(
                self.page,
                self.login_url,
                credential.username,
                credential.password,
                self.consignataria,
            ):
                raise AdapterError(
                    OutcomeKind.RETRYABLE_ERROR,
                    "Login RF1 não foi confirmado após as tentativas configuradas.",
                    code="rf1_login_not_confirmed",
                    retry_after_seconds=900,
                )
            self._ensure_query_page()
        except Exception:
            self.close()
            raise

    def _ensure_query_page(self) -> None:
        if LOGIN_PATH.lower() in self.page.url.lower():
            if not _login(
                self.page,
                self.login_url,
                self.credential.username,
                self.credential.password,
                self.consignataria,
            ):
                raise AdapterError(
                    OutcomeKind.CREDENTIAL_ERROR,
                    "Não foi possível renovar a sessão RF1.",
                    code="rf1_session_login_failed",
                )
        if self.query_url not in self.page.url:
            self.page.goto(self.query_url, wait_until="domcontentloaded")
        if LOGIN_PATH.lower() in self.page.url.lower():
            raise AdapterError(
                OutcomeKind.CREDENTIAL_ERROR,
                "A sessão RF1 expirou ao abrir a consulta.",
                code="rf1_session_expired",
            )
        self.page.wait_for_selector(
            "#ctl00_ctl00_ContentPlaceHolder1_ContentPlaceHolder1_btnListar",
            timeout=20_000,
        )

    def consult(self, item: WorkItem) -> ExecutionOutcome:
        self._ensure_query_page()
        raw = _consultar(self.page, item.cpf)
        expected = _digits(item.cpf)
        confirmed_cpf = _digits(raw.get("CPF_Retornado"))
        if confirmed_cpf != expected:
            raise AdapterError(
                OutcomeKind.RETRYABLE_ERROR,
                "O RF1 não confirmou o CPF solicitado.",
                code="rf1_identifier_mismatch",
            )
        expected_registration = _registration(item.registration)
        returned_registration = _registration(raw.get("Matricula"))
        if expected_registration and returned_registration != expected_registration:
            raise AdapterError(
                OutcomeKind.RETRYABLE_ERROR,
                "O RF1 retornou matrícula diferente da solicitada.",
                code="rf1_registration_mismatch",
            )
        person_keys = {
            "Nome",
            "Matricula",
            "Data_Nascimento",
            "Regime_Trabalho",
            "Relacao_Trabalho",
            "Categoria",
            "Data_Admissao",
            "Situacao_Ativo",
        }
        margin_keys = {
            key
            for key in raw
            if "Margem" in key or key in {"Cartao_Consignado", "Salario_Base"}
        }
        return ExecutionOutcome.found(
            requested=item.requested,
            confirmed={"cpf": confirmed_cpf, "registration": raw.get("Matricula")},
            person={key: raw.get(key) for key in person_keys},
            margins={key: raw.get(key) for key in margin_keys},
            raw=raw,
        )

    def close(self) -> None:
        page = getattr(self, "page", None)
        if page is not None:
            try:
                _logout(page)
            except Exception:
                pass
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


class RF1Adapter:
    platform = "rf1"
    version = "rf1.v1"
    batch_size = 1
    lease_seconds = 600

    def open_session(self, credential: CredentialPayload) -> RF1Session:
        return RF1Session(credential)

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
        if isinstance(exc, RF1NotFound):
            return ExecutionOutcome.not_found(
                requested=requested,
                raw={"Status_Robo": "Não Encontrado"},
            )
        if isinstance(exc, PlaywrightTimeoutError):
            return ExecutionOutcome.error(
                OutcomeKind.RETRYABLE_ERROR,
                requested=requested,
                code="rf1_timeout",
                message="RF1 não confirmou a resposta no tempo limite.",
                stage=stage,
                end_session=True,
                raw={"Status_Robo": "Timeout"},
            )
        if isinstance(exc, RF1Error):
            kind = OutcomeKind.RETRYABLE_ERROR
            if stage == "login":
                kind = OutcomeKind.CREDENTIAL_ERROR
            elif "CPF inválido" in str(exc):
                kind = OutcomeKind.PERMANENT_ERROR
            return ExecutionOutcome.error(
                kind,
                requested=requested,
                code="rf1_error",
                message=str(exc)[:500],
                stage=stage,
                end_session=kind in {
                    OutcomeKind.CREDENTIAL_ERROR,
                    OutcomeKind.RETRYABLE_ERROR,
                },
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
            code="rf1_unexpected_error",
            message=str(exc)[:500],
            stage=stage,
            retry_after_seconds=60,
            end_session=True,
        )

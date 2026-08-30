"""Adapter transacional SAFE/Secretaria de Fortaleza.

O adapter conhece apenas a sessão do portal. Fila, retries, agenda, arquivos e
notificações continuam sob responsabilidade do ``GenericWorker`` e do backend.
"""

from __future__ import annotations

import os
import re
from typing import Any

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from machine_admin.secret_store import get_runtime_secret
from services.captcha import CaptchaError, resolve_turnstile
from services.execution import ExecutionOutcome, OutcomeKind
from services.proxy import HttpProxy, parse_http_proxy
from workers.engine import AdapterError, CredentialPayload, PortalSession, WorkItem


DEFAULT_LOGIN_URL = "https://fortaleza.safeconsig.com.br/safe/login"
DEFAULT_QUERY_URL = "https://fortaleza.safeconsig.com.br/safe/pages/consulta/margem/"

LOGIN_FIELD = '[id="idLogin"]'
PASSWORD_FIELD = '[id="senhaUsuario"]'
LOGIN_BUTTON = '[id="loginButtom"]'
REGISTRATION_FIELD = (
    '[id="tabView:pesquisaMutuario:j_idt412:input"], '
    'input[id$="pesquisaMutuario:j_idt412:input"]'
)
CPF_FIELD = (
    '[id="tabView:pesquisaMutuario:j_idt416:j_idt418"], '
    'input[id$="pesquisaMutuario:j_idt418"], '
    '[id="tabView:pesquisaMutuario:j_idt414:j_idt416"], '
    'input[id$="pesquisaMutuario:j_idt414:j_idt416"]'
)
SEARCH_BUTTON = (
    '[id="tabView:pesquisaMutuario:j_idt422"], '
    'button[id$="pesquisaMutuario:j_idt422"], '
    '[id="tabView:pesquisaMutuario:j_idt420"], '
    'button[id$="pesquisaMutuario:j_idt420"]'
)
RESULT_TABLE = 'tbody[id="tabView:pesquisaMutuario:listaColaborador:input_data"]'
DETAIL_PANEL = "div.grid-colaborador"
BROWSER_VIEWPORT = {"width": 1280, "height": 900}


def _proxy_settings() -> HttpProxy:
    try:
        return parse_http_proxy(get_runtime_secret("SAFECONSIG_HTTP_PROXY"))
    except (RuntimeError, ValueError) as exc:
        raise AdapterError(
            OutcomeKind.INTEGRATION_UNAVAILABLE,
            "Proxy SafeConsig ausente ou inválida.",
            code="safeconsig_proxy_invalid",
            retry_after_seconds=900,
        ) from exc


class SafeConsigResponseUnconfirmed(RuntimeError):
    """O portal respondeu, mas a tela não permite confirmar o resultado."""


class SafeConsigPortalUnavailable(RuntimeError):
    """O SAFE respondeu com indisponibilidade HTTP explícita."""


def _digits(value: object) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _registration(value: object) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _visible(page: Page, selector: str) -> bool:
    try:
        return page.locator(selector).first.is_visible()
    except PlaywrightError:
        return False


def _body_text(page: Page) -> str:
    try:
        return page.locator("body").inner_text(timeout=3_000)
    except PlaywrightError:
        return ""


def _turnstile_visible(page: Page) -> bool:
    selectors = (
        ".cf-turnstile",
        'iframe[src*="challenges.cloudflare.com"]',
        'iframe[src*="turnstile"]',
        'input[name="cf-turnstile-response"]',
    )
    return any(_visible(page, selector) for selector in selectors)


def _turnstile_present(page: Page) -> bool:
    """Detecta o widget mesmo depois de o JSF ocultá-lo após um POST falho."""
    selectors = (
        ".cf-turnstile[data-sitekey]",
        'input[name="cf-turnstile-response"]',
        'iframe[src*="challenges.cloudflare.com"]',
        'iframe[src*="turnstile"]',
    )
    for selector in selectors:
        try:
            if page.locator(selector).count() > 0:
                return True
        except PlaywrightError:
            continue
    return False


def _turnstile_token_ready(page: Page) -> bool:
    """Confirma se o próprio widget já produziu um token utilizável."""
    try:
        field = page.locator('input[name="cf-turnstile-response"]').first
        return bool(field.count() and str(field.input_value() or "").strip())
    except PlaywrightError:
        return False


def _explicit_invalid_credentials(text: str) -> bool:
    normalized = text.casefold()
    markers = (
        "usuário ou senha inválid",
        "usuario ou senha invalid",
        "senha inválida",
        "senha invalida",
        "credenciais inválidas",
        "credenciais invalidas",
        "acesso não autorizado",
    )
    return any(marker in normalized for marker in markers)


def _explicit_not_found(text: str) -> bool:
    normalized = " ".join(text.casefold().split())
    markers = (
        "nenhum registro",
        "nenhum resultado encontrado",
        "não foram encontrados registros",
        "nao foram encontrados registros",
    )
    return any(marker in normalized for marker in markers)


def _labeled_value(text: str, *labels: str) -> str:
    for label in labels:
        match = re.search(
            rf"(?:^|\n)\s*{re.escape(label)}\s*:?\s*(?:\n\s*)?([^\n]+)",
            text,
            flags=re.IGNORECASE,
        )
        if match:
            value = match.group(1).strip(" :-\t")
            if value:
                return value
    return ""


def _first_text(page: Page, selector: str) -> str:
    try:
        locator = page.locator(selector).first
        if locator.is_visible():
            return locator.inner_text().strip()
    except PlaywrightError:
        pass
    return ""


def _detail_payload(page: Page) -> dict[str, str]:
    detail = page.locator(DETAIL_PANEL).first
    text = detail.inner_text(timeout=5_000).strip()
    cpf = _first_text(
        page,
        'div.grid-colaborador div.ui-grid-row:has(span:has-text("CPF:")) '
        ".ui-grid-col-3",
    ) or _labeled_value(text, "CPF")
    registration = _first_text(
        page,
        'div.grid-colaborador div.ui-grid-row:has(span:has-text("Matrícula:")) '
        ".ui-grid-col-3",
    ) or _labeled_value(text, "Matrícula", "Matricula")
    margin = _first_text(
        page,
        'div.grid-colaborador tr:has(span:has-text("Margem Líquida '
        '(Valor Disponível):")) td:nth-child(2)',
    ) or _labeled_value(text, "Margem Líquida (Valor Disponível)")
    return {
        "CPF_Confirmado": cpf,
        "Matricula": registration,
        "Nome": _labeled_value(text, "Nome", "Servidor"),
        "Vinculo": _labeled_value(text, "Vínculo", "Vinculo"),
        "Secretaria": _labeled_value(text, "Secretaria"),
        "Cargo": _labeled_value(text, "Cargo"),
        "Margem Emprestimo": margin,
        "Status_Robo": "Sucesso",
    }


def _found_outcome(item: WorkItem, raw: dict[str, Any]) -> ExecutionOutcome:
    expected_cpf = _digits(item.cpf)
    confirmed_cpf = _digits(raw.get("CPF_Confirmado"))
    if confirmed_cpf != expected_cpf:
        raise AdapterError(
            OutcomeKind.RETRYABLE_ERROR,
            "O SAFE não confirmou o CPF solicitado na ficha retornada.",
            code="safeconsig_cpf_unconfirmed",
        )

    expected_registration = _registration(item.registration)
    confirmed_registration = _registration(raw.get("Matricula"))
    if not expected_registration:
        raise AdapterError(
            OutcomeKind.PERMANENT_ERROR,
            "Fortaleza exige matrícula na base de entrada.",
            code="safeconsig_registration_required",
            end_session=False,
        )
    if confirmed_registration != expected_registration:
        raise AdapterError(
            OutcomeKind.RETRYABLE_ERROR,
            "O SAFE retornou matrícula diferente da solicitada.",
            code="safeconsig_registration_mismatch",
        )
    if not str(raw.get("Margem Emprestimo") or "").strip():
        raise SafeConsigResponseUnconfirmed(
            "A ficha SAFE não exibiu a margem líquida esperada."
        )

    person = {
        key: raw.get(key)
        for key in ("Nome", "Matricula", "Vinculo", "Secretaria", "Cargo")
        if raw.get(key) not in (None, "")
    }
    return ExecutionOutcome.found(
        requested=item.requested,
        confirmed={
            "cpf": confirmed_cpf,
            "registration": raw.get("Matricula"),
        },
        person=person,
        margins={"Margem Emprestimo": raw.get("Margem Emprestimo")},
        raw=raw,
    )


class SafeConsigSession(PortalSession):
    def __init__(self, credential: CredentialPayload) -> None:
        self.credential = credential
        self.login_url = credential.login_url or DEFAULT_LOGIN_URL
        self.query_url = credential.query_url or DEFAULT_QUERY_URL
        self._playwright = self.browser = self.context = self.page = None
        try:
            self.proxy = _proxy_settings()
            self._playwright = sync_playwright().start()
            self.browser = self._playwright.chromium.launch(
                headless=os.getenv("HEADLESS", "true").lower() == "true",
                proxy=self.proxy.playwright_settings(),
            )
            self.context = self.browser.new_context(viewport=BROWSER_VIEWPORT)
            self.page = self.context.new_page()
            self._login()
        except Exception:
            self.close()
            raise

    def _check_http_response(self, response: Any, *, stage: str) -> None:
        if response is None:
            return
        status = int(getattr(response, "status", 0) or 0)
        if status >= 500:
            raise SafeConsigPortalUnavailable(
                f"O SAFE respondeu HTTP {status} durante {stage}."
            )

    def _reject_visible_turnstile(self) -> None:
        assert self.page is not None
        if _turnstile_visible(self.page):
            raise AdapterError(
                OutcomeKind.INTEGRATION_UNAVAILABLE,
                "O login SAFE exige Cloudflare Turnstile; resolução automática ainda não homologada.",
                code="safeconsig_turnstile_required",
                retry_after_seconds=900,
            )

    def _login(self) -> None:
        assert self.page is not None
        response = self.page.goto(
            self.login_url, wait_until="domcontentloaded", timeout=30_000
        )
        self._check_http_response(response, stage="login")
        self.page.locator(LOGIN_FIELD).wait_for(state="visible", timeout=15_000)
        self.page.locator(PASSWORD_FIELD).wait_for(state="visible", timeout=15_000)
        self.page.fill(LOGIN_FIELD, self.credential.username)
        self.page.fill(PASSWORD_FIELD, self.credential.password)
        # O widget Managed do Cloudflare normalmente confirma sozinho. Dar a ele
        # uma janela curta e submeter o formulário evita consumir 2Captcha sem
        # necessidade. Só classificamos integração indisponível se o portal
        # permanecer no login depois da tentativa.
        self.page.wait_for_timeout(2_500)
        captcha_attempted = False
        if (
            _turnstile_present(self.page)
            and not _turnstile_token_ready(self.page)
        ):
            # Não envie um POST sabidamente sem token. Além de ser inútil, ele
            # pode vincular o ViewState/cookie da sessão a uma tentativa negada.
            self._solve_turnstile_and_submit()
            captcha_attempted = True
        else:
            try:
                self._submit_login(timeout=7_000)
            except PlaywrightTimeoutError:
                # Em headless, o botão pode ficar bloqueado até o Turnstile ser
                # resolvido. Nesse caso não espere o timeout global nem repita o
                # captcha: faça uma única tentativa assistida.
                if (
                    _visible(self.page, LOGIN_FIELD)
                    and _turnstile_present(self.page)
                ):
                    self._solve_turnstile_and_submit()
                    captcha_attempted = True
                else:
                    raise
        body = _body_text(self.page)
        if _explicit_invalid_credentials(body):
            raise AdapterError(
                OutcomeKind.CREDENTIAL_ERROR,
                "Usuário ou senha recusados pelo SAFE.",
                code="safeconsig_invalid_credentials",
            )
        if (
            not captcha_attempted
            and _visible(self.page, LOGIN_FIELD)
            and _turnstile_present(self.page)
        ):
            self._solve_turnstile_and_submit()
            body = _body_text(self.page)
            if _explicit_invalid_credentials(body):
                raise AdapterError(
                    OutcomeKind.CREDENTIAL_ERROR,
                    "Usuário ou senha recusados pelo SAFE.",
                    code="safeconsig_invalid_credentials",
                )
        if _visible(self.page, LOGIN_FIELD):
            raise AdapterError(
                OutcomeKind.RETRYABLE_ERROR,
                "O SAFE não confirmou uma sessão autenticada.",
                code="safeconsig_login_not_confirmed",
                retry_after_seconds=300,
            )
        self._dismiss_contact_update()
        self._open_query_page(login_stage=True)

    def _submit_login(self, *, timeout: int) -> None:
        assert self.page is not None
        self.page.click(LOGIN_BUTTON, timeout=timeout)
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=20_000)
        except PlaywrightTimeoutError:
            # A confirmação positiva fora do formulário é a autoridade; o
            # chamador valida a tela logo depois.
            pass
        self.page.wait_for_timeout(1_000)

    def _solve_turnstile_and_submit(self) -> None:
        assert self.page is not None
        solution = resolve_turnstile(
            self.page,
            '#idForm12344 .cf-turnstile',
            proxy=self.proxy,
        )
        if solution.user_agent:
            # O token do Turnstile é vinculado ao User-Agent usado pelo solver.
            # Recriar o contexto faz com que GET, cookies, JavaScript e POST
            # usem esse mesmo agente desde o início. Alterá-lo só no POST deixa
            # uma sessão incoerente e o SAFE rejeita mesmo um token válido.
            previous_context = self.context
            replacement = self.browser.new_context(
                viewport=BROWSER_VIEWPORT,
                user_agent=solution.user_agent,
            )
            replacement_page = replacement.new_page()
            response = replacement_page.goto(
                self.login_url, wait_until="domcontentloaded", timeout=30_000
            )
            self._check_http_response(response, stage="login")
            replacement_page.locator(LOGIN_FIELD).wait_for(
                state="visible", timeout=15_000
            )
            replacement_page.locator(PASSWORD_FIELD).wait_for(
                state="visible", timeout=15_000
            )
            self.context = replacement
            self.page = replacement_page
            try:
                previous_context.close()
            except PlaywrightError:
                pass
        self.page.evaluate(
            """token => {
                const form = document.getElementById('idForm12344');
                if (!form) return;
                for (const name of ['cf-turnstile-response', 'g-recaptcha-response']) {
                    let input = form.querySelector(`[name="${name}"]`);
                    if (!input) {
                        input = document.createElement('input');
                        input.type = 'hidden';
                        input.name = name;
                        form.appendChild(input);
                    }
                    input.value = token;
                    input.setAttribute('value', token);
                    input.dispatchEvent(new Event('input', {bubbles: true}));
                    input.dispatchEvent(new Event('change', {bubbles: true}));
                }
            }""",
            solution.token,
        )
        # O primeiro POST pode redesenhar o formulário; preenche novamente antes
        # da submissão com o token recém-resolvido.
        self.page.fill(LOGIN_FIELD, self.credential.username)
        self.page.fill(PASSWORD_FIELD, self.credential.password)
        self._submit_login(timeout=20_000)

    def _dismiss_contact_update(self) -> bool:
        assert self.page is not None
        button = self.page.locator('button:has-text("Cadastrar Depois")').first
        try:
            if not button.is_visible():
                return False
            button.click()
            self.page.wait_for_timeout(500)
            return True
        except PlaywrightError:
            return False

    def _open_query_page(self, *, login_stage: bool = False) -> None:
        assert self.page is not None
        response = self.page.goto(
            self.query_url, wait_until="domcontentloaded", timeout=30_000
        )
        self._check_http_response(response, stage="consulta")
        if self._dismiss_contact_update():
            response = self.page.goto(
                self.query_url, wait_until="domcontentloaded", timeout=30_000
            )
            self._check_http_response(response, stage="consulta")
        if _visible(self.page, LOGIN_FIELD):
            raise AdapterError(
                OutcomeKind.RETRYABLE_ERROR,
                (
                    "O SAFE não confirmou uma sessão autenticada."
                    if login_stage
                    else "A sessão SAFE expirou durante a consulta."
                ),
                code=(
                    "safeconsig_login_not_confirmed"
                    if login_stage
                    else "safeconsig_session_expired"
                ),
                retry_after_seconds=300 if login_stage else 60,
            )
        # Um redirect de sessão expirada também redesenha o Turnstile. Ele deve
        # ser tratado acima como retry de sessão, e não como falha da integração
        # ou da credencial. Fora do login, um desafio inesperado continua sendo
        # evidência de que a resposta da consulta não pode ser confirmada.
        self._reject_visible_turnstile()
        try:
            self.page.locator(REGISTRATION_FIELD).first.wait_for(
                state="visible", timeout=15_000
            )
            self.page.locator(CPF_FIELD).first.wait_for(
                state="visible", timeout=15_000
            )
            self.page.locator(SEARCH_BUTTON).first.wait_for(
                state="visible", timeout=15_000
            )
        except PlaywrightTimeoutError as exc:
            raise SafeConsigResponseUnconfirmed(
                "O formulário de consulta SAFE não foi reconhecido."
            ) from exc

    def _wait_search_state(self) -> str:
        assert self.page is not None
        for _ in range(30):
            if _visible(self.page, DETAIL_PANEL):
                return "detail"
            if _visible(self.page, RESULT_TABLE):
                rows = self.page.locator(f"{RESULT_TABLE} tr")
                if rows.count() > 0:
                    return "table"
            if _explicit_not_found(_body_text(self.page)):
                return "not_found"
            self.page.wait_for_timeout(500)
        raise SafeConsigResponseUnconfirmed(
            "O SAFE não apresentou resultado positivo nem negativo explícito."
        )

    def _wait_primefaces_idle(self) -> None:
        """Aguarda o callback AJAX aplicar o HTML antes de ler a tabela.

        A tabela inicial já contém a frase negativa padrão. Ler o DOM logo no
        evento ``response`` pode, portanto, confundir esse conteúdo antigo com
        o resultado da consulta recém-enviada.
        """
        assert self.page is not None
        try:
            self.page.wait_for_function(
                """() => {
                    const queue = window.PrimeFaces?.ajax?.Queue;
                    return !queue || (
                        typeof queue.isEmpty === 'function' && queue.isEmpty()
                    );
                }""",
                timeout=10_000,
            )
            # O callback da fila agenda a atualização do widget no mesmo ciclo;
            # esta pequena janela deixa a mutação do DOM finalizar.
            self.page.wait_for_timeout(250)
        except PlaywrightTimeoutError as exc:
            raise SafeConsigResponseUnconfirmed(
                "O AJAX da consulta SAFE não terminou de atualizar a página."
            ) from exc

    def _select_registration(self, expected_registration: str) -> bool:
        assert self.page is not None
        rows = self.page.locator(f"{RESULT_TABLE} tr")
        saw_explicit_registration = False
        for index in range(rows.count()):
            row = rows.nth(index)
            text = row.inner_text().strip()
            if _explicit_not_found(text):
                return False
            cells = row.locator("td")
            registration = ""
            if cells.count() >= 3:
                registration = cells.nth(2).inner_text().strip()
            if not registration:
                registration = _labeled_value(text, "Matrícula", "Matricula")
            if not _registration(registration):
                continue
            saw_explicit_registration = True
            if _registration(registration) != expected_registration:
                continue
            button = row.locator("button").first
            if not button.is_visible():
                raise SafeConsigResponseUnconfirmed(
                    "A matrícula foi localizada, mas a ação de consulta não está disponível."
                )
            button.click()
            self.page.locator(DETAIL_PANEL).first.wait_for(
                state="visible", timeout=15_000
            )
            return True
        if not saw_explicit_registration:
            raise SafeConsigResponseUnconfirmed(
                "A tabela SAFE foi exibida, mas suas matrículas não puderam ser lidas."
            )
        return False

    def consult(self, item: WorkItem) -> ExecutionOutcome:
        expected_cpf = _digits(item.cpf)
        expected_registration = _registration(item.registration)
        if len(expected_cpf) != 11:
            raise AdapterError(
                OutcomeKind.PERMANENT_ERROR,
                "CPF inválido na base de entrada.",
                code="invalid_cpf",
                end_session=False,
            )
        if not expected_registration:
            raise AdapterError(
                OutcomeKind.PERMANENT_ERROR,
                "Fortaleza exige matrícula na base de entrada.",
                code="safeconsig_registration_required",
                end_session=False,
            )

        self._open_query_page()
        assert self.page is not None
        registration_field = self.page.locator(REGISTRATION_FIELD).first
        cpf_field = self.page.locator(CPF_FIELD).first
        registration_field.fill(str(item.registration or "").strip())
        cpf_field.fill(expected_cpf)
        with self.page.expect_response(
            lambda response: "/consulta/margem" in response.url.casefold(),
            timeout=20_000,
        ) as response_info:
            self.page.locator(SEARCH_BUTTON).first.click()
        response = response_info.value
        response.finished()
        self._check_http_response(response, stage="consulta")
        self._wait_primefaces_idle()
        self._reject_visible_turnstile()
        if _digits(cpf_field.input_value()) != expected_cpf:
            raise SafeConsigResponseUnconfirmed(
                "O CPF enviado ao SAFE mudou durante a consulta."
            )
        if _registration(registration_field.input_value()) != expected_registration:
            raise SafeConsigResponseUnconfirmed(
                "A matrícula enviada ao SAFE mudou durante a consulta."
            )

        state = self._wait_search_state()
        if state == "not_found":
            return ExecutionOutcome.not_found(
                requested=item.requested,
                raw={"Status_Robo": "Não Encontrado", "Evidencia": "Nenhum registro"},
            )
        if state == "table" and not self._select_registration(expected_registration):
            return ExecutionOutcome.not_found(
                requested=item.requested,
                raw={
                    "Status_Robo": "Não Encontrado",
                    "Evidencia": "Matrícula não retornada para o CPF consultado",
                },
            )
        raw = _detail_payload(self.page)
        return _found_outcome(item, raw)

    def close(self) -> None:
        for resource_name in ("context", "browser"):
            resource = getattr(self, resource_name, None)
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    pass
                setattr(self, resource_name, None)
        playwright = getattr(self, "_playwright", None)
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:
                pass
            self._playwright = None


class SafeConsigAdapter:
    platform = "safeconsig"
    version = "safeconsig.v1"
    batch_size = 1
    lease_seconds = 600

    def open_session(self, credential: CredentialPayload) -> SafeConsigSession:
        return SafeConsigSession(credential)

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
        if isinstance(exc, CaptchaError):
            return ExecutionOutcome.error(
                OutcomeKind.INTEGRATION_UNAVAILABLE,
                requested=requested,
                code="safeconsig_turnstile_unavailable",
                message=str(exc)[:500],
                stage=stage,
                retry_after_seconds=900,
                end_session=True,
            )
        if isinstance(exc, SafeConsigResponseUnconfirmed):
            return ExecutionOutcome.error(
                OutcomeKind.RETRYABLE_ERROR,
                requested=requested,
                code="safeconsig_response_unconfirmed",
                message=str(exc)[:500],
                stage=stage,
                retry_after_seconds=60,
                end_session=True,
            )
        if isinstance(exc, PlaywrightTimeoutError):
            return ExecutionOutcome.error(
                OutcomeKind.RETRYABLE_ERROR,
                requested=requested,
                code="safeconsig_timeout",
                message=str(exc)[:500] or "Tempo limite na consulta SAFE.",
                stage=stage,
                retry_after_seconds=60,
                end_session=True,
                raw={"Status_Robo": "Timeout"},
            )
        if isinstance(exc, SafeConsigPortalUnavailable):
            return ExecutionOutcome.error(
                OutcomeKind.PORTAL_UNAVAILABLE,
                requested=requested,
                code="safeconsig_portal_unavailable",
                message=str(exc)[:500],
                stage=stage,
                retry_after_seconds=300,
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
            code="safeconsig_unexpected_error",
            message=str(exc)[:500],
            stage=stage,
            retry_after_seconds=60,
            end_session=True,
        )

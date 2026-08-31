"""Contrato do adapter SAFE sem navegador ou portal reais."""

from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from services.captcha import CaptchaError, TurnstileSolution, resolve_turnstile
from services.execution import OutcomeKind
from services.proxy import PortalProxy
from services.registry import MUNICIPALITIES
from workers.adapters.safeconsig import (
    LOGIN_FIELD,
    SafeConsigAdapter,
    SafeConsigResponseUnconfirmed,
    SafeConsigSession,
    _explicit_not_found,
    _found_outcome,
    _labeled_value,
    _proxy_settings,
    _turnstile_present,
    _turnstile_token_ready,
)
from workers.engine import AdapterError, WorkItem
from workers.registry import ADAPTERS, create_adapter


class SafeConsigAdapterTests(unittest.TestCase):
    def test_fortaleza_is_testing_with_transactional_contract(self) -> None:
        definition = MUNICIPALITIES["fortaleza"]

        self.assertEqual("safeconsig.v1", definition.adapter_version)
        self.assertEqual("testing", definition.operational_status)
        self.assertEqual(
            ["cpf", "registration"], definition.input_schema["required"]
        )
        self.assertEqual(
            ["cpf", "registration"],
            definition.input_schema["deduplication_key"],
        )
        self.assertEqual(
            "https://fortaleza.safeconsig.com.br/safe/login",
            definition.login_url,
        )
        self.assertEqual(
            "https://fortaleza.safeconsig.com.br/safe/pages/consulta/margem/",
            definition.query_url,
        )
        self.assertTrue(ADAPTERS["safeconsig"].available)
        self.assertIsInstance(create_adapter("safeconsig"), SafeConsigAdapter)

    def test_found_requires_confirmed_cpf_registration_and_margin(self) -> None:
        item = WorkItem(
            item_id=1, cpf="012.345.678-90", registration="AB-123"
        )
        outcome = _found_outcome(
            item,
            {
                "CPF_Confirmado": "012.345.678-90",
                "Matricula": "AB 123",
                "Nome": "Pessoa",
                "Margem Emprestimo": "R$ 500,00",
                "Status_Robo": "Sucesso",
            },
        )

        self.assertEqual(OutcomeKind.FOUND, outcome.kind)
        self.assertEqual("01234567890", outcome.confirmed["cpf"])
        self.assertEqual("AB 123", outcome.confirmed["registration"])
        self.assertEqual("R$ 500,00", outcome.margins["Margem Emprestimo"])

        bad_payloads = (
            {
                "CPF_Confirmado": "99999999999",
                "Matricula": "AB 123",
                "Margem Emprestimo": "R$ 500,00",
            },
            {
                "CPF_Confirmado": "01234567890",
                "Matricula": "OUTRA",
                "Margem Emprestimo": "R$ 500,00",
            },
        )
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(AdapterError):
                    _found_outcome(item, payload)

        with self.assertRaises(SafeConsigResponseUnconfirmed):
            _found_outcome(
                item,
                {
                    "CPF_Confirmado": "01234567890",
                    "Matricula": "AB123",
                    "Margem Emprestimo": "",
                },
            )

    def test_registration_is_mandatory_and_does_not_end_the_session(self) -> None:
        item = WorkItem(item_id=1, cpf="01234567890", registration=None)
        with self.assertRaises(AdapterError) as raised:
            _found_outcome(
                item,
                {
                    "CPF_Confirmado": "01234567890",
                    "Matricula": "ABC",
                    "Margem Emprestimo": "0,00",
                },
            )
        self.assertEqual(OutcomeKind.PERMANENT_ERROR, raised.exception.kind)
        self.assertFalse(raised.exception.end_session)

    def test_only_explicit_negative_copy_is_not_found_evidence(self) -> None:
        self.assertTrue(_explicit_not_found("Nenhum registro encontrado"))
        self.assertTrue(_explicit_not_found("Nenhum resultado encontrado"))
        self.assertFalse(_explicit_not_found("A consulta demorou para responder"))
        self.assertFalse(_explicit_not_found("Erro inesperado no formulário"))

    def test_detail_labels_are_parsed_without_selector_assumptions(self) -> None:
        text = (
            "CPF:\n012.345.678-90\n"
            "Matrícula:\nAB-123\n"
            "Margem Líquida (Valor Disponível):\nR$ 42,00"
        )
        self.assertEqual("012.345.678-90", _labeled_value(text, "CPF"))
        self.assertEqual("AB-123", _labeled_value(text, "Matrícula"))
        self.assertEqual(
            "R$ 42,00",
            _labeled_value(text, "Margem Líquida (Valor Disponível)"),
        )

    def test_timeout_and_unknown_html_are_retryable_never_not_found(self) -> None:
        adapter = SafeConsigAdapter()
        item = WorkItem(item_id=1, cpf="01234567890", registration="ABC")
        cases = (
            PlaywrightTimeoutError("timeout"),
            SafeConsigResponseUnconfirmed("HTML desconhecido"),
        )
        for error in cases:
            with self.subTest(error=type(error).__name__):
                outcome = adapter.classify_exception(
                    error, stage="consultation", item=item
                )
                self.assertEqual(OutcomeKind.RETRYABLE_ERROR, outcome.kind)
                self.assertNotEqual(OutcomeKind.NOT_FOUND, outcome.kind)
                self.assertTrue(outcome.end_session)

    def test_turnstile_error_preserves_integration_category(self) -> None:
        adapter = SafeConsigAdapter()
        error = AdapterError(
            OutcomeKind.INTEGRATION_UNAVAILABLE,
            "Turnstile necessário.",
            code="safeconsig_turnstile_required",
            retry_after_seconds=900,
        )

        outcome = adapter.classify_exception(error, stage="login")

        self.assertEqual(OutcomeKind.INTEGRATION_UNAVAILABLE, outcome.kind)
        self.assertEqual("safeconsig_turnstile_required", outcome.error_code)
        self.assertEqual(900, outcome.retry_after_seconds)

        unavailable = adapter.classify_exception(
            CaptchaError("2Captcha indisponível"), stage="login"
        )
        self.assertEqual(OutcomeKind.INTEGRATION_UNAVAILABLE, unavailable.kind)
        self.assertEqual(
            "safeconsig_turnstile_unavailable", unavailable.error_code
        )

    def test_hidden_turnstile_is_still_present_after_failed_post(self) -> None:
        class Locator:
            def __init__(self, count: int):
                self._count = count

            def count(self) -> int:
                return self._count

        class Page:
            def locator(self, selector: str) -> Locator:
                return Locator(1 if selector == '.cf-turnstile[data-sitekey]' else 0)

        self.assertTrue(_turnstile_present(Page()))

    def test_managed_turnstile_token_is_detected_before_login_post(self) -> None:
        field = Mock()
        field.first = field
        field.count.return_value = 1
        field.input_value.return_value = "managed-token"
        page = Mock()
        page.locator.return_value = field

        self.assertTrue(_turnstile_token_ready(page))

        field.input_value.return_value = ""
        self.assertFalse(_turnstile_token_ready(page))

    def test_turnstile_solver_accepts_attached_hidden_widget(self) -> None:
        class Widget:
            first = None

            def __init__(self):
                self.first = self
                self.wait_state = None

            def wait_for(self, *, state: str, timeout: int) -> None:
                self.wait_state = (state, timeout)

            def get_attribute(self, name: str) -> str | None:
                return "site-key" if name == "data-sitekey" else None

        class Page:
            url = "https://fortaleza.safeconsig.com.br/safe/login"

            def __init__(self):
                self.widget = Widget()

            def locator(self, _selector: str) -> Widget:
                return self.widget

            def evaluate(self, expression: str) -> str:
                self.assert_expression = expression
                return "Browser User Agent"

        submitted = Mock()
        submitted.raise_for_status.return_value = None
        submitted.json.return_value = {"status": 1, "request": "captcha-id"}
        solved = Mock()
        solved.raise_for_status.return_value = None
        solved.json.return_value = {
            "status": 1,
            "request": "token",
            "useragent": "Solver User Agent",
        }
        page = Page()

        with (
            patch("services.captcha.get_runtime_secret", return_value="api-key"),
            patch(
                "services.captcha.requests.post", return_value=submitted
            ) as post_request,
            patch("services.captcha.requests.get", return_value=solved),
            patch("services.captcha.time.sleep"),
        ):
            solution = resolve_turnstile(
                page,
                proxy=PortalProxy(
                    "proxy.example",
                    10000,
                    username="worker",
                    password="secret",
                ),
            )

        self.assertEqual("token", solution.token)
        self.assertEqual("Solver User Agent", solution.user_agent)
        self.assertEqual(("attached", 10_000), page.widget.wait_state)
        self.assertEqual(
            "Browser User Agent",
            post_request.call_args.kwargs["data"]["userAgent"],
        )
        self.assertEqual(
            "worker:secret@proxy.example:10000",
            post_request.call_args.kwargs["data"]["proxy"],
        )
        self.assertEqual(
            "HTTP", post_request.call_args.kwargs["data"]["proxytype"]
        )

    def test_solver_user_agent_recreates_context_before_submit(self) -> None:
        session = SafeConsigSession.__new__(SafeConsigSession)
        session.login_url = "https://fortaleza.safeconsig.com.br/safe/login"
        session.credential = Mock(username="usuario", password="senha")
        old_context = Mock()
        old_page = Mock()
        replacement_context = Mock()
        replacement_page = Mock()
        replacement_page.evaluate.return_value = "Solver User Agent"
        replacement_context.new_page.return_value = replacement_page
        session.context = old_context
        session.page = old_page
        session.browser = Mock()
        session.proxy = PortalProxy(
            "proxy.example", 10000, username="worker", password="secret"
        )
        session.browser.new_context.return_value = replacement_context

        with (
            patch(
                "workers.adapters.safeconsig.resolve_turnstile",
                return_value=TurnstileSolution(
                    token="token", user_agent="Solver User Agent"
                ),
            ) as resolve_turnstile_mock,
            patch.object(session, "_check_http_response"),
            patch.object(session, "_submit_login") as submit,
        ):
            session._solve_turnstile_and_submit()

        session.browser.new_context.assert_called_once_with(
            viewport={"width": 1280, "height": 900},
            user_agent="Solver User Agent",
        )
        old_context.close.assert_called_once_with()
        self.assertIs(replacement_context, session.context)
        self.assertIs(replacement_page, session.page)
        self.assertIs(
            session.proxy,
            resolve_turnstile_mock.call_args.kwargs["proxy"],
        )
        self.assertEqual(2, resolve_turnstile_mock.call_count)
        submit.assert_called_once_with(timeout=20_000)

    def test_safeconsig_proxy_is_optional_but_invalid_value_is_explicit(self) -> None:
        with patch(
            "workers.adapters.safeconsig.get_runtime_secret",
            side_effect=RuntimeError("not configured"),
        ):
            self.assertIsNone(_proxy_settings())

        with patch(
            "workers.adapters.safeconsig.get_runtime_secret",
            return_value="",
        ):
            self.assertIsNone(_proxy_settings())

        with patch(
            "workers.adapters.safeconsig.get_runtime_secret",
            return_value="invalid",
        ):
            with self.assertRaises(AdapterError) as raised:
                _proxy_settings()
        self.assertEqual("safeconsig_proxy_invalid", raised.exception.code)

    def test_query_redirect_to_login_is_retryable_session_expiry(self) -> None:
        class LoginRedirectPage:
            def goto(self, *_args, **_kwargs):
                return None

        session = SafeConsigSession.__new__(SafeConsigSession)
        session.page = LoginRedirectPage()
        session.query_url = "https://fortaleza.safeconsig.com.br/safe/pages/consulta/margem/"

        with (
            patch.object(session, "_dismiss_contact_update", return_value=False),
            patch.object(
                session,
                "_reject_visible_turnstile",
                side_effect=AssertionError("Turnstile must be checked after login redirect"),
            ),
            patch(
                "workers.adapters.safeconsig._visible",
                side_effect=lambda _page, selector: selector == LOGIN_FIELD,
            ),
        ):
            with self.assertRaises(AdapterError) as raised:
                session._open_query_page()

        self.assertEqual(OutcomeKind.RETRYABLE_ERROR, raised.exception.kind)
        self.assertEqual("safeconsig_session_expired", raised.exception.code)
        self.assertEqual(60, raised.exception.retry_after_seconds)


if __name__ == "__main__":
    unittest.main()

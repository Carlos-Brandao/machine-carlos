"""Regressões do contrato estrito do ConsigX/Itabuna."""

from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from consiglog.consiglog import (
    ConsiglogError,
    ConsiglogResponseUnconfirmed,
    _choose_profile_index,
    _configured_login_profile,
    _consult,
    _has_substantive_result,
    _pick_explicit_cpf,
    _validate_confirmed_result,
)
from services.execution import OutcomeKind
from workers.adapters.consiglog import ConsiglogAdapter
from workers.engine import WorkItem


class ConsiglogProfileTests(unittest.TestCase):
    def test_profile_supports_current_and_legacy_setting_names(self) -> None:
        self.assertEqual(
            "Perfil atual",
            _configured_login_profile(
                {
                    "portal_profile": "Perfil atual",
                    "consignataria": "Perfil legado",
                    "servico": "Serviço",
                }
            ),
        )
        self.assertEqual(
            "Perfil legado",
            _configured_login_profile({"consignataria": "Perfil legado"}),
        )
        self.assertEqual(
            "Serviço legado",
            _configured_login_profile({"servico": "Serviço legado"}),
        )

    def test_configured_profile_selects_matching_option_not_first(self) -> None:
        choices = [
            "FINANCEIRA - OUTRA [111]",
            "FINANCEIRA - SOMAPAY [64994;6499] Entrar",
        ]
        self.assertEqual(
            1,
            _choose_profile_index("FINANCEIRA SOMAPAY 64994 6499", choices),
        )

    def test_multiple_profiles_require_explicit_configuration(self) -> None:
        with self.assertRaisesRegex(ConsiglogError, "mais de um perfil"):
            _choose_profile_index(None, ["Perfil A", "Perfil B"])

    def test_missing_or_ambiguous_profile_is_not_silently_accepted(self) -> None:
        with self.assertRaisesRegex(ConsiglogError, "não foi encontrado"):
            _choose_profile_index("Perfil C", ["Perfil A", "Perfil B"])
        with self.assertRaisesRegex(ConsiglogError, "mais de uma opção"):
            _choose_profile_index(
                "SOMAPAY",
                ["SOMAPAY Perfil 1", "SOMAPAY Perfil 2"],
            )


class ConsiglogResponseTests(unittest.TestCase):
    CPF = "79097596220"

    def test_search_input_is_never_used_as_result_confirmation(self) -> None:
        candidates = [
            {
                "id": "body_cpfTextBox",
                "value": self.CPF,
                "editable": False,
                "visible": True,
            },
            {
                "id": "body_resultadoCpfLabel",
                "value": self.CPF,
                "editable": False,
                "visible": True,
            },
        ]
        self.assertEqual(self.CPF, _pick_explicit_cpf(candidates))
        self.assertEqual("", _pick_explicit_cpf(candidates[:1]))

    def test_editable_or_hidden_cpf_is_not_confirmation(self) -> None:
        candidates = [
            {"id": "cpfAux", "value": self.CPF, "editable": True, "visible": True},
            {"id": "cpfHidden", "value": self.CPF, "editable": False, "visible": False},
        ]
        self.assertEqual("", _pick_explicit_cpf(candidates))

    def test_found_requires_explicit_cpf_registration_and_margin(self) -> None:
        valid = {
            "Matricula": "12345",
            "MARGEM EMPRESTIMO DISPONIVEL": "R$ 0,00",
        }
        self.assertTrue(_has_substantive_result(valid))
        self.assertEqual(
            self.CPF,
            _validate_confirmed_result(self.CPF, valid, self.CPF),
        )

        with self.assertRaisesRegex(ConsiglogResponseUnconfirmed, "sem exibir o CPF"):
            _validate_confirmed_result(self.CPF, valid, "")
        with self.assertRaisesRegex(ConsiglogResponseUnconfirmed, "difere"):
            _validate_confirmed_result(self.CPF, valid, "79097596221")
        with self.assertRaisesRegex(ConsiglogResponseUnconfirmed, "matrícula e margem"):
            _validate_confirmed_result(self.CPF, {"Matricula": "12345"}, self.CPF)
        with self.assertRaisesRegex(ConsiglogResponseUnconfirmed, "matrícula e margem"):
            _validate_confirmed_result(
                self.CPF,
                {"MARGEM EMPRESTIMO DISPONIVEL": "R$ 100,00"},
                self.CPF,
            )

    def test_unconfirmed_response_is_retryable_and_never_not_found(self) -> None:
        item = WorkItem(item_id=1, cpf=self.CPF, registration=None)
        outcome = ConsiglogAdapter().classify_exception(
            ConsiglogResponseUnconfirmed("Resposta incompleta."),
            stage="consultation",
            item=item,
        )
        self.assertEqual(OutcomeKind.RETRYABLE_ERROR, outcome.kind)
        self.assertEqual("consigx_response_unconfirmed", outcome.error_code)
        self.assertTrue(outcome.end_session)

    def test_consult_waits_for_state_newer_than_the_pre_click_snapshot(self) -> None:
        page = Mock()
        page.evaluate.return_value = '{"registration":"","margins":[],"cpfs":[],"modals":[]}'
        cpf_field, search_button = Mock(), Mock()
        page.locator.side_effect = lambda selector: (
            cpf_field if selector == "#body_cpfTextBox" else search_button
        )
        with (
            patch("consiglog.consiglog._wait_for_fresh_response") as wait,
            patch(
                "consiglog.consiglog._dismiss_modal",
                return_value="CPF/Matrícula não encontrado.",
            ),
        ):
            result = _consult(page, self.CPF, None)

        cpf_field.fill.assert_called_once_with(self.CPF)
        search_button.click.assert_called_once_with()
        wait.assert_called_once_with(page, page.evaluate.return_value)
        self.assertEqual("Não Encontrado", result["Status_Robo"])


if __name__ == "__main__":
    unittest.main()

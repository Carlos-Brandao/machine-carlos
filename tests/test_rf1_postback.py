"""Regressões do postback CPF -> matrícula/órgão no RF1 Boa Vista."""

from __future__ import annotations

import unittest

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from rf1.rf1 import (
    RF1NotFound,
    _CPF_DEPENDENCIES_READY,
    _CPF_POSTBACK_OBSERVED,
    _PFXO,
    _await_cpf_dependencies,
)


class _FakeLocator:
    def __init__(self, page: "_FakePage", selector: str) -> None:
        self.page = page
        self.selector = selector

    def click(self) -> None:
        self.page.events.append(("click", self.selector))

    def inner_text(self) -> str:
        return self.page.body_text


class _FakePage:
    def __init__(
        self,
        *,
        wait_results: list[Exception | None],
        snapshots: list[dict] | None = None,
        body_text: str = "",
        body_after_wait: list[str | None] | None = None,
        postback_observed: bool = False,
    ) -> None:
        self.wait_results = list(wait_results)
        self.snapshots = list(
            snapshots
            or [
                {
                    "cpf": "11111111111",
                    "matricula": "MATRICULA-ANTIGA",
                    "orgao": "ORGAO-ANTIGO",
                    "viewState": "VIEWSTATE-ANTIGO",
                }
            ]
        )
        self.body_text = body_text
        self.body_after_wait = list(body_after_wait or [])
        self.postback_observed = postback_observed
        self.events: list[tuple] = []
        self.wait_calls: list[tuple[str, list, int]] = []
        self.reloads = 0

    def wait_for_selector(self, selector: str, *, timeout: int) -> None:
        self.events.append(("wait_for_selector", selector, timeout))

    def evaluate(self, script: str, arg: list) -> dict | bool:
        self.events.append(("evaluate", arg))
        if script == _CPF_POSTBACK_OBSERVED:
            return self.postback_observed
        return self.snapshots.pop(0)

    def fill(self, selector: str, value: str) -> None:
        self.events.append(("fill", selector, value))

    def locator(self, selector: str) -> _FakeLocator:
        return _FakeLocator(self, selector)

    def wait_for_function(self, script: str, *, arg: list, timeout: int) -> None:
        self.events.append(("wait_for_function",))
        self.wait_calls.append((script, arg, timeout))
        result = self.wait_results.pop(0)
        if self.body_after_wait:
            replacement = self.body_after_wait.pop(0)
            if replacement is not None:
                self.body_text = replacement
        if result is not None:
            raise result

    def wait_for_timeout(self, milliseconds: int) -> None:
        self.events.append(("wait_for_timeout", milliseconds))

    def reload(self, *, wait_until: str, timeout: int) -> None:
        self.reloads += 1
        self.events.append(("reload", wait_until, timeout))


class RF1PostbackTests(unittest.TestCase):
    CPF = "79097596220"

    def test_previous_dependencies_are_part_of_postback_confirmation(self) -> None:
        old = {
            "cpf": "11111111111",
            "matricula": "MATRICULA-ANTIGA",
            "orgao": "ORGAO-ANTIGO",
            "viewState": "VIEWSTATE-ANTIGO",
        }
        page = _FakePage(wait_results=[None], snapshots=[old])

        _await_cpf_dependencies(page, self.CPF)  # type: ignore[arg-type]

        script, arg, timeout = page.wait_calls[0]
        self.assertEqual(_CPF_DEPENDENCIES_READY, script)
        self.assertEqual(self.CPF, arg[3])
        self.assertEqual(old, arg[5])
        self.assertEqual(15_000, timeout)
        self.assertIn("dependenciesChanged", script)
        self.assertIn("rf1PostbackProbe", script)

        fill_index = next(i for i, event in enumerate(page.events) if event[0] == "fill")
        blur_index = next(i for i, event in enumerate(page.events) if event[0] == "click")
        wait_index = next(
            i for i, event in enumerate(page.events) if event[0] == "wait_for_function"
        )
        self.assertLess(fill_index, blur_index)
        self.assertLess(blur_index, wait_index)
        self.assertEqual(f"{_PFXO}txtMatricula", page.events[blur_index][1])

    def test_timeout_reloads_once_and_repeats_the_human_flow(self) -> None:
        timeout = PlaywrightTimeoutError("postback travado")
        page = _FakePage(
            wait_results=[timeout, None],
            snapshots=[
                {
                    "cpf": "11111111111",
                    "matricula": "OLD-1",
                    "orgao": "ORG-1",
                    "viewState": "VS-1",
                },
                {
                    "cpf": "",
                    "matricula": "",
                    "orgao": "",
                    "viewState": "VS-2",
                },
            ],
        )

        _await_cpf_dependencies(page, self.CPF)  # type: ignore[arg-type]

        self.assertEqual(1, page.reloads)
        self.assertEqual(2, len(page.wait_calls))
        self.assertEqual(
            2,
            sum(1 for event in page.events if event[0] == "fill"),
        )
        self.assertEqual(
            2,
            sum(1 for event in page.events if event[0] == "click"),
        )

    def test_second_timeout_is_retryable_instead_of_false_not_found(self) -> None:
        page = _FakePage(
            wait_results=[
                PlaywrightTimeoutError("primeiro timeout"),
                PlaywrightTimeoutError("segundo timeout"),
            ],
            snapshots=[
                {"cpf": "", "matricula": "", "orgao": "", "viewState": "1"},
                {"cpf": "", "matricula": "", "orgao": "", "viewState": "2"},
            ],
        )

        with self.assertRaises(PlaywrightTimeoutError):
            _await_cpf_dependencies(page, self.CPF)  # type: ignore[arg-type]
        self.assertEqual(1, page.reloads)

    def test_explicit_portal_evidence_still_returns_not_found_without_reload(self) -> None:
        page = _FakePage(
            wait_results=[PlaywrightTimeoutError("sem dependências")],
            body_after_wait=["Servidor não encontrado para o CPF informado."],
        )

        with self.assertRaises(RF1NotFound):
            _await_cpf_dependencies(page, self.CPF)  # type: ignore[arg-type]
        self.assertEqual(0, page.reloads)

    def test_stale_not_found_message_never_classifies_the_next_cpf(self) -> None:
        timeout = PlaywrightTimeoutError("postback sem resposta")
        page = _FakePage(
            wait_results=[timeout, timeout],
            snapshots=[
                {"cpf": "", "matricula": "", "orgao": "", "viewState": "1"},
                {"cpf": "", "matricula": "", "orgao": "", "viewState": "1"},
            ],
            body_text="Servidor não encontrado para a consulta anterior.",
            postback_observed=False,
        )

        with self.assertRaises(PlaywrightTimeoutError):
            _await_cpf_dependencies(page, self.CPF)  # type: ignore[arg-type]
        self.assertEqual(1, page.reloads)


if __name__ == "__main__":
    unittest.main()

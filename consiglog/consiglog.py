"""Consulta de margem no ConsigX com navegador e sessão WebForms preservada.

O portal depende de cookies, ViewState e etapas de autenticação; por isso este
runner deliberadamente não tenta reproduzir a consulta com ``requests``.
"""

from __future__ import annotations

import os
import re
import signal
import sys
import traceback
import unicodedata
from pathlib import Path

import pandas as pd
from playwright.sync_api import Page, TimeoutError, sync_playwright

sys.path.insert(0, str(Path(__file__).parent.parent))
from services.utils import aguardar_enter, mask_cpf


DEFAULT_LOGIN_URL = "https://saec.consigx.com.br/Login.aspx"
DEFAULT_QUERY_URL = "https://saec.consigx.com.br/Margem/ConsultaMargem.aspx"
LOGIN_SECOND_STEP = "LoginSegundaEtapa.aspx"
LOGIN_SELECTION = "LoginSelecao.aspx"

CPF_FIELD = "#body_cpfTextBox"
SEARCH_BUTTON = "#body_pesquisarButton"
CANCEL_BUTTON = "#body_cancelarButton"
SERVICE_SELECT = "#body_servicoDropDownList"
NOT_FOUND_TEXT = "CPF/Matrícula não encontrado."

RESULT_COLUMNS = (
    "Matricula",
    "Categoria",
    "Lotacao",
    "Situacao",
    "MARGEM EMPRESTIMO TOTAL",
    "MARGEM EMPRESTIMO RESERVADA",
    "MARGEM EMPRESTIMO DISPONIVEL",
    "MARGEM BENEFICIO COMPRA TOTAL",
    "MARGEM BENEFICIO COMPRA RESERVADA",
    "MARGEM BENEFICIO COMPRA DISPONIVEL",
    "MARGEM BENEFICIO SAQUE TOTAL",
    "MARGEM BENEFICIO SAQUE RESERVADA",
    "MARGEM BENEFICIO SAQUE DISPONIVEL",
    "MARGEM EVENTUAIS TOTAL",
    "MARGEM EVENTUAIS RESERVADA",
    "MARGEM EVENTUAIS DISPONIVEL",
    "MARGEM BENEFICIO TOTAL",
    "MARGEM BENEFICIO RESERVADA",
    "MARGEM BENEFICIO DISPONIVEL",
    "Status_Robo",
)


class ConsiglogError(RuntimeError):
    """Falha conhecida de sessão, portal ou configuração ConsigX."""


def _digits(value: object) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _normalise(text: str) -> str:
    return "".join(
        char
        for char in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(char)
    ).upper().strip()


def _save(records: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.stem}.tmp{path.suffix}")
    pd.DataFrame(records).to_excel(temporary, index=False)
    temporary.replace(path)


def _visible(page: Page, selector: str) -> bool:
    locator = page.locator(selector)
    return locator.count() > 0 and locator.first.is_visible()


def _dismiss_modal(page: Page) -> str:
    """Fecha uma modal do portal e devolve sua mensagem, quando houver."""
    modal = page.locator("[role=dialog], [id*='AjaxModal'], [id*='Popup']")
    for index in range(modal.count()):
        candidate = modal.nth(index)
        if not candidate.is_visible():
            continue
        message = candidate.inner_text().strip()
        button = candidate.locator("input[type=button], input[type=submit], button")
        if button.count() and button.first.is_visible():
            button.first.click()
        return message
    return ""


def _login(page: Page, login_url: str, usuario: str, senha: str) -> None:
    page.goto(login_url, wait_until="domcontentloaded")
    for _ in range(5):
        current_url = page.url.lower()
        if LOGIN_SECOND_STEP.lower() in current_url:
            page.locator("#txtSenha").fill(senha)
            with page.expect_navigation(wait_until="domcontentloaded", timeout=25_000):
                page.locator("#Entrar").click()
            continue
        if LOGIN_SELECTION.lower() in current_url:
            choice = page.locator("input[id*='imgEntrar']")
            if not choice.count():
                raise ConsiglogError("O portal não exibiu uma consignatária selecionável.")
            with page.expect_navigation(wait_until="domcontentloaded", timeout=25_000):
                choice.first.click()
            continue
        if _visible(page, "#txtLogin"):
            page.locator("#txtLogin").fill(usuario)
            with page.expect_navigation(wait_until="domcontentloaded", timeout=25_000):
                page.locator("#Entrar").click()
            continue
        if "erro.aspx" in current_url:
            page.goto(login_url, wait_until="domcontentloaded")
            continue
        return
    raise ConsiglogError("O login no ConsigX não concluiu após cinco tentativas.")


def _read_value(page: Page, selector: str) -> str:
    locator = page.locator(selector)
    if not locator.count():
        return ""
    return locator.first.evaluate(
        "element => (element.value ?? element.innerText ?? '').trim()"
    )


def _extract(page: Page) -> dict[str, str]:
    data = {column: "" for column in RESULT_COLUMNS if column != "Status_Robo"}
    data["Matricula"] = _read_value(page, "[id*='matriculaTextBox']")
    data["Categoria"] = _read_value(page, "[id*='categoriaTextBox']")
    data["Lotacao"] = _read_value(page, "[id*='txtLotacao']")
    data["Situacao"] = _read_value(page, "[id*='txtSituacao']")

    for index in range(12):
        row = page.locator(f"[id*='headerservico_{index}']")
        if not row.count():
            continue
        values = [part.strip() for part in row.first.inner_text().splitlines() if part.strip()]
        if len(values) < 4:
            values = [part.strip() for part in row.first.inner_text().split("\t") if part.strip()]
        if len(values) < 4:
            continue
        service, total, reserved, available = values[:4]
        service = _normalise(service)
        if "EMPRESTIMO" in service:
            prefix = "MARGEM EMPRESTIMO"
        elif "BENEFICIO COMPRA" in service:
            prefix = "MARGEM BENEFICIO COMPRA"
        elif "BENEFICIO SAQUE" in service:
            prefix = "MARGEM BENEFICIO SAQUE"
        elif "EVENTUAIS" in service:
            prefix = "MARGEM EVENTUAIS"
        elif "BENEFICIO" in service:
            prefix = "MARGEM BENEFICIO"
        else:
            continue
        data[f"{prefix} TOTAL"] = total
        data[f"{prefix} RESERVADA"] = reserved
        data[f"{prefix} DISPONIVEL"] = available
    return data


def _consult(page: Page, cpf: str, service: str | None) -> dict[str, str]:
    if len(cpf) != 11:
        raise ConsiglogError("CPF inválido na planilha de entrada.")
    page.locator(CPF_FIELD).fill(cpf)
    if service:
        page.locator(SERVICE_SELECT).select_option(service)
    page.locator(SEARCH_BUTTON).click()
    page.wait_for_timeout(1_000)

    modal_text = _dismiss_modal(page)
    if NOT_FOUND_TEXT.casefold() in modal_text.casefold():
        return {"Status_Robo": "Não Encontrado"}
    if "erro" in modal_text.casefold():
        raise ConsiglogError(modal_text)

    if page.locator("[id*='matriculaTextBox'], [id*='headerservico_']").count():
        data = _extract(page)
        data["Status_Robo"] = "Sucesso"
        return data
    raise ConsiglogError("O portal não confirmou nem retornou dados após a consulta.")


def main(config: dict, input_file: Path, temp_file: Path, output_file: Path) -> None:
    usuario, senha = config.get("usuario", "").strip(), config.get("senha", "")
    if not usuario or not senha:
        raise ConsiglogError("Usuário e senha do Itabuna não foram configurados.")
    login_url = config.get("url_login", DEFAULT_LOGIN_URL).strip()
    query_url = config.get("url_consulta", DEFAULT_QUERY_URL).strip()
    service = config.get("servico", "").strip() or None

    source = pd.read_excel(input_file, dtype=str)
    cpf_column = next((column for column in source.columns if column.upper() == "CPF"), None)
    if cpf_column is None:
        raise ConsiglogError("A planilha de entrada precisa ter a coluna CPF.")
    cpfs = list(dict.fromkeys(cpf for cpf in source[cpf_column].map(_digits) if cpf))
    records = pd.read_excel(temp_file, dtype=str).to_dict("records") if temp_file.exists() else []
    processed = {_digits(item.get("CPF_Chave")) for item in records}
    pending = [cpf for cpf in cpfs if cpf not in processed]
    print(f"{len(records)} processados, {len(pending)} pendentes.")

    stopped = False
    previous_handler = signal.getsignal(signal.SIGINT)

    def stop_after_current(*_args: object) -> None:
        nonlocal stopped
        stopped = True
        print("Interrupção recebida; salvando após o CPF atual.")

    signal.signal(signal.SIGINT, stop_after_current)
    headless = os.getenv("HEADLESS", "false").lower() == "true"
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=headless)
            page = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
            try:
                _login(page, login_url, usuario, senha)
                for position, cpf in enumerate(pending, start=1):
                    if stopped:
                        break
                    print(f"[{position}/{len(pending)}] CPF: {mask_cpf(cpf)}")
                    try:
                        page.goto(query_url, wait_until="domcontentloaded")
                        if _visible(page, "#txtLogin"):
                            _login(page, login_url, usuario, senha)
                            page.goto(query_url, wait_until="domcontentloaded")
                        page.locator(CPF_FIELD).wait_for(state="visible", timeout=20_000)
                        result = _consult(page, cpf, service)
                    except TimeoutError:
                        result = {"Status_Robo": "Timeout"}
                    except Exception as error:
                        print(f"  erro: {type(error).__name__}: {error}")
                        result = {"Status_Robo": f"Erro: {type(error).__name__}"}
                    result["CPF_Chave"] = cpf
                    records.append(result)
                    _save(records, temp_file)
            finally:
                browser.close()
    finally:
        signal.signal(signal.SIGINT, previous_handler)
        if not headless:
            aguardar_enter()

    if stopped:
        print(f"Parcial salvo em: {temp_file}")
        return
    result_frame = pd.DataFrame(records)
    final = source.assign(_cpf=source[cpf_column].map(_digits)).merge(
        result_frame, left_on="_cpf", right_on="CPF_Chave", how="left"
    ).drop(columns=["_cpf", "CPF_Chave"], errors="ignore")
    final.to_excel(output_file, index=False)
    temp_file.unlink(missing_ok=True)
    print(f"{len(records)} consultados → {output_file}")

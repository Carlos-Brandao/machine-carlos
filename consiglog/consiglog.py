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
from collections.abc import Mapping, Sequence
from pathlib import Path

import pandas as pd
import requests
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, TimeoutError, sync_playwright

sys.path.insert(0, str(Path(__file__).parent.parent))
from machine_admin.secret_store import get_runtime_secret
from services.utils import aguardar_enter, mask_cpf


DEFAULT_LOGIN_URL = "https://saec.consigx.com.br/Login.aspx"
DEFAULT_QUERY_URL = "https://saec.consigx.com.br/Margem/ConsultaMargem.aspx"
LOGIN_SECOND_STEP = "LoginSegundaEtapa.aspx"
LOGIN_SELECTION = "LoginSelecao.aspx"

CPF_FIELD = "#body_cpfTextBox"
SEARCH_BUTTON = (
    "#body_pesquisarButton, input[id*='pesquisarButton'], "
    "input[id*='btnConsultar'], input[name*='btnConsultar']"
)
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


class ConsiglogPortalUnavailable(ConsiglogError):
    """A origem de execução não alcança o portal ConsigX."""


class ConsiglogResponseUnconfirmed(ConsiglogError):
    """A consulta terminou sem evidência suficiente para um resultado final."""


def _digits(value: object) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _normalise(text: str) -> str:
    return "".join(
        char
        for char in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(char)
    ).upper().strip()


def _match_key(value: object) -> str:
    """Normaliza rótulos do portal sem depender de pontuação ou acentos."""
    return " ".join(re.findall(r"[A-Z0-9]+", _normalise(str(value or ""))))


def _configured_login_profile(settings: Mapping[str, object]) -> str | None:
    """Resolve o perfil mantendo compatibilidade com os três nomes históricos."""
    for key in ("portal_profile", "consignataria", "servico"):
        value = str(settings.get(key) or "").strip()
        if value:
            return value
    return None


def _choose_profile_index(
    configured_profile: str | None,
    choice_labels: Sequence[str],
) -> int:
    """Escolhe uma entrada da tela de perfis sem assumir silenciosamente a primeira."""
    if not choice_labels:
        raise ConsiglogError("O portal não exibiu um perfil selecionável.")
    if not configured_profile:
        if len(choice_labels) == 1:
            return 0
        raise ConsiglogError(
            "O ConsigX exibiu mais de um perfil. Configure o campo "
            "'Perfil no portal' para selecionar o acesso correto."
        )

    expected = _match_key(configured_profile)
    if not expected:
        raise ConsiglogError("O perfil configurado para o ConsigX está vazio.")

    ranked: list[tuple[int, int]] = []
    for index, label in enumerate(choice_labels):
        candidate = _match_key(label)
        if not candidate:
            continue
        if candidate == expected:
            score = 3
        elif expected in candidate:
            score = 2
        elif len(candidate) >= 4 and candidate in expected:
            score = 1
        else:
            continue
        ranked.append((score, index))

    if not ranked:
        raise ConsiglogError(
            f"O perfil configurado '{configured_profile}' não foi encontrado no ConsigX."
        )
    best_score = max(score for score, _ in ranked)
    matches = [index for score, index in ranked if score == best_score]
    if len(matches) != 1:
        raise ConsiglogError(
            f"O perfil configurado '{configured_profile}' corresponde a mais de uma opção."
        )
    return matches[0]


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


def _profile_choice_label(choice: object) -> str:
    """Obtém o menor contêiner textual que identifica um botão de perfil."""
    return str(
        choice.evaluate(  # type: ignore[attr-defined]
            """element => {
                const preferred = [
                    element.closest('[data-profile]'),
                    element.closest('[data-perfil]'),
                    element.closest('tr'),
                    element.closest('li'),
                    element.closest('fieldset'),
                    element.closest('article')
                ].filter(Boolean);
                let node = element.parentElement;
                for (let depth = 0; node && depth < 4; depth += 1) {
                    preferred.push(node);
                    node = node.parentElement;
                }
                const texts = preferred
                    .map(candidate => (candidate.innerText || candidate.textContent || '').trim())
                    .filter(Boolean)
                    .sort((left, right) => left.length - right.length);
                const own = [element.value, element.alt, element.title]
                    .filter(Boolean).join(' ');
                return [texts[0] || '', own].filter(Boolean).join(' ');
            }"""
        )
        or ""
    ).strip()


def _select_login_profile(page: Page, configured_profile: str | None) -> None:
    choices = page.locator("input[id*='imgEntrar']")
    visible_choices = [
        choices.nth(index)
        for index in range(choices.count())
        if choices.nth(index).is_visible()
    ]
    labels = [_profile_choice_label(choice) for choice in visible_choices]
    selected = visible_choices[_choose_profile_index(configured_profile, labels)]
    selected.click()


def _login(
    page: Page,
    login_url: str,
    usuario: str,
    senha: str,
    portal_profile: str | None = None,
) -> None:
    if not get_runtime_secret("CONSIGX_HTTPS_PROXY"):
        try:
            session = requests.Session()
            session.trust_env = False
            response = session.get(login_url, timeout=(5, 15))
            if response.status_code >= 500:
                raise requests.RequestException(f"HTTP {response.status_code}")
        except requests.RequestException as exc:
            raise ConsiglogPortalUnavailable(
                "Portal ConsigX inacessível pela rede deste worker. "
                "Solicite a liberação do IP de saída da VPS no portal."
            ) from exc
    for attempt in range(1, 4):
        try:
            page.goto(login_url, wait_until="domcontentloaded", timeout=60_000)
            break
        except (TimeoutError, PlaywrightError):
            if _visible(page, "#txtLogin"):
                break
            if attempt == 3:
                raise ConsiglogPortalUnavailable(
                    "Portal ConsigX indisponível a partir desta origem após três tentativas de login."
                )
            page.wait_for_timeout(2_000)
    for _ in range(5):
        current_url = page.url.lower()
        _dismiss_modal(page)
        if _visible(page, "#txtSenha"):
            page.locator("#txtSenha").fill(senha)
            page.locator("#Entrar").click()
            page.wait_for_timeout(500)
            continue
        choice = page.locator("input[id*='imgEntrar']")
        if choice.count():
            _select_login_profile(page, portal_profile)
            page.wait_for_timeout(500)
            continue
        if _visible(page, "#txtLogin"):
            page.locator("#txtLogin").fill(usuario)
            page.locator("#Entrar").click()
            page.wait_for_timeout(500)
            continue
        if "erro.aspx" in current_url:
            page.goto(login_url, wait_until="domcontentloaded", timeout=60_000)
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


def _pick_explicit_cpf(candidates: Sequence[Mapping[str, object]]) -> str:
    """Aceita somente CPF exibido na resposta; o campo de busca nunca confirma nada."""
    for candidate in candidates:
        element_id = str(candidate.get("id") or "").strip().casefold()
        if element_id == CPF_FIELD.removeprefix("#").casefold():
            continue
        if bool(candidate.get("editable")) or not bool(candidate.get("visible", True)):
            continue
        value = _digits(candidate.get("value"))
        if len(value) == 11:
            return value
    return ""


def _read_explicit_response_cpf(page: Page) -> str:
    candidates = page.evaluate(
        """() => {
            const direct = [...document.querySelectorAll(
                "[id*='cpf' i], [name*='cpf' i], [data-field*='cpf' i], " +
                "[id*='resultado' i] span, [id*='resultado' i] td, " +
                "[id*='dados' i] span, [id*='dados' i] td"
            )];
            return [...new Set(direct)].map(element => {
                const style = window.getComputedStyle(element);
                const tag = element.tagName.toUpperCase();
                const editable = (tag === 'INPUT' || tag === 'TEXTAREA')
                    && !element.readOnly && !element.disabled;
                return {
                    id: element.id || '',
                    name: element.getAttribute('name') || '',
                    value: (element.value ?? element.innerText ?? element.textContent ?? '').trim(),
                    editable,
                    visible: style.display !== 'none' && style.visibility !== 'hidden'
                        && element.getClientRects().length > 0
                };
            });
        }"""
    )
    return _pick_explicit_cpf(candidates if isinstance(candidates, list) else [])


def _has_substantive_result(data: Mapping[str, object]) -> bool:
    registration = str(data.get("Matricula") or "").strip()
    has_margin = any(
        str(value or "").strip()
        for key, value in data.items()
        if str(key).startswith("MARGEM ")
    )
    return bool(registration and has_margin)


def _validate_confirmed_result(
    expected_cpf: str,
    data: Mapping[str, object],
    explicit_cpf: str,
) -> str:
    """Valida identidade e substância antes de autorizar um resultado ``found``."""
    confirmed_cpf = _digits(explicit_cpf)
    if len(confirmed_cpf) != 11:
        raise ConsiglogResponseUnconfirmed(
            "O ConsigX retornou uma ficha sem exibir o CPF de resposta. "
            "A consulta será tentada novamente para evitar falso positivo."
        )
    if confirmed_cpf != _digits(expected_cpf):
        raise ConsiglogResponseUnconfirmed(
            "O CPF exibido na resposta do ConsigX difere do CPF solicitado."
        )
    if not _has_substantive_result(data):
        raise ConsiglogResponseUnconfirmed(
            "O ConsigX não retornou uma ficha substantiva com matrícula e margem."
        )
    return confirmed_cpf


_RESPONSE_STATE_SCRIPT = """() => {
    const read = element => (element?.value ?? element?.innerText ?? '').trim();
    const visible = element => {
        if (!element) return false;
        const style = window.getComputedStyle(element);
        return style.display !== 'none' && style.visibility !== 'hidden'
            && element.getClientRects().length > 0;
    };
    const registration = read(document.querySelector("[id*='matriculaTextBox']"));
    const margins = [...document.querySelectorAll("[id*='headerservico_']")]
        .filter(visible).map(read).filter(Boolean);
    const cpfs = [...document.querySelectorAll(
        "[id*='cpf' i], [name*='cpf' i], [data-field*='cpf' i], " +
        "[id*='resultado' i] span, [id*='resultado' i] td, " +
        "[id*='dados' i] span, [id*='dados' i] td"
    )].filter(element => element.id !== 'body_cpfTextBox' && visible(element))
      .map(read).filter(Boolean);
    const modals = [...document.querySelectorAll(
        "[role=dialog], [id*='AjaxModal'], [id*='Popup']"
    )].filter(visible).map(read).filter(Boolean);
    return JSON.stringify({registration, margins, cpfs, modals});
}"""


def _wait_for_fresh_response(page: Page, previous_state: str, timeout: int = 20_000) -> None:
    page.wait_for_function(
        """before => {
            const read = element => (element?.value ?? element?.innerText ?? '').trim();
            const visible = element => {
                if (!element) return false;
                const style = window.getComputedStyle(element);
                return style.display !== 'none' && style.visibility !== 'hidden'
                    && element.getClientRects().length > 0;
            };
            const registration = read(document.querySelector("[id*='matriculaTextBox']"));
            const margins = [...document.querySelectorAll("[id*='headerservico_']")]
                .filter(visible).map(read).filter(Boolean);
            const cpfs = [...document.querySelectorAll(
                "[id*='cpf' i], [name*='cpf' i], [data-field*='cpf' i], " +
                "[id*='resultado' i] span, [id*='resultado' i] td, " +
                "[id*='dados' i] span, [id*='dados' i] td"
            )].filter(element => element.id !== 'body_cpfTextBox' && visible(element))
              .map(read).filter(Boolean);
            const modals = [...document.querySelectorAll(
                "[role=dialog], [id*='AjaxModal'], [id*='Popup']"
            )].filter(visible).map(read).filter(Boolean);
            const current = JSON.stringify({registration, margins, cpfs, modals});
            if (current === before) return false;
            const parsed = JSON.parse(current);
            return Boolean(parsed.registration || parsed.margins.length
                || parsed.cpfs.length || parsed.modals.length);
        }""",
        arg=previous_state,
        timeout=timeout,
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


def _select_configured_service(page: Page, configured_service: str) -> None:
    select = page.locator(SERVICE_SELECT)
    if not select.count() or not select.first.is_visible():
        raise ConsiglogError(
            "O serviço foi configurado, mas o ConsigX não exibiu sua seleção."
        )
    options = select.first.locator("option")
    expected = _match_key(configured_service)
    matches: list[tuple[int, str]] = []
    for index in range(options.count()):
        option = options.nth(index)
        value = str(option.get_attribute("value") or "")
        label = str(option.inner_text() or "")
        value_key, label_key = _match_key(value), _match_key(label)
        if expected in {value_key, label_key}:
            score = 3
        elif expected and (expected in label_key or expected in value_key):
            score = 2
        elif label_key and label_key in expected:
            score = 1
        else:
            continue
        matches.append((score, value))
    if not matches:
        raise ConsiglogError(
            f"O serviço configurado '{configured_service}' não foi encontrado no ConsigX."
        )
    best_score = max(score for score, _ in matches)
    values = list(dict.fromkeys(value for score, value in matches if score == best_score))
    if len(values) != 1:
        raise ConsiglogError(
            f"O serviço configurado '{configured_service}' corresponde a mais de uma opção."
        )
    select.first.select_option(value=values[0])


def _consult(page: Page, cpf: str, service: str | None) -> dict[str, str]:
    if len(cpf) != 11:
        raise ConsiglogError("CPF inválido na planilha de entrada.")
    previous_state = str(page.evaluate(_RESPONSE_STATE_SCRIPT))
    page.locator(CPF_FIELD).fill(cpf)
    if service:
        _select_configured_service(page, service)
    page.locator(SEARCH_BUTTON).click()
    _wait_for_fresh_response(page, previous_state)

    modal_text = _dismiss_modal(page)
    if _normalise(NOT_FOUND_TEXT) in _normalise(modal_text):
        return {"Status_Robo": "Não Encontrado"}
    if modal_text:
        raise ConsiglogResponseUnconfirmed(modal_text)

    data = _extract(page)
    confirmed_cpf = _validate_confirmed_result(
        cpf,
        data,
        _read_explicit_response_cpf(page),
    )
    data["CPF_Confirmado"] = confirmed_cpf
    data["Status_Robo"] = "Sucesso"
    return data


def main(config: dict, input_file: Path, temp_file: Path, output_file: Path) -> None:
    usuario, senha = config.get("usuario", "").strip(), config.get("senha", "")
    if not usuario or not senha:
        raise ConsiglogError("Usuário e senha do Itabuna não foram configurados.")
    login_url = config.get("url_login", DEFAULT_LOGIN_URL).strip()
    query_url = config.get("url_consulta", DEFAULT_QUERY_URL).strip()
    service = config.get("servico", "").strip() or None
    portal_profile = _configured_login_profile(config)

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
                _login(page, login_url, usuario, senha, portal_profile)
                for position, cpf in enumerate(pending, start=1):
                    if stopped:
                        break
                    print(f"[{position}/{len(pending)}] CPF: {mask_cpf(cpf)}")
                    try:
                        page.goto(query_url, wait_until="domcontentloaded")
                        if _visible(page, "#txtLogin"):
                            _login(page, login_url, usuario, senha, portal_profile)
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

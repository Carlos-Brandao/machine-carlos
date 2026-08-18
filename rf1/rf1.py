import os
import re
import secrets
import signal
import sys
import traceback
import unicodedata
from pathlib import Path

import pandas as pd
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, sync_playwright, TimeoutError

sys.path.insert(0, str(Path(__file__).parent.parent))
from services.captcha import _solve_2captcha
from services.utils import aguardar_enter, mask_cpf


_PFXO = "#ctl00_ctl00_ContentPlaceHolder1_ContentPlaceHolder1_"
_PFXL = "#ctl00_ContentPlaceHolder1_"
DEFAULT_LOGIN_URL = "https://boavista.rf1consig.com.br/SGConsignataria/ConsigAcessoUsuarioLogar.aspx"
DEFAULT_QUERY_URL = "https://boavista.rf1consig.com.br/SGConsignataria/GESTOR/CADPessoaListar.aspx"
LOGIN_PATH = "ConsigAcessoUsuarioLogar.aspx"
LOGIN_ATTEMPTS = 5


_ARM_CPF_POSTBACK_PROBE = r"""([cpfSelector, matriculaSelector, orgaoSelector, token]) => {
    const cpf = document.querySelector(cpfSelector);
    const matricula = document.querySelector(matriculaSelector);
    const orgao = document.querySelector(orgaoSelector);
    const viewState = document.querySelector("input[name='__VIEWSTATE']");
    if (!cpf || !matricula || !orgao) {
        return null;
    }
    cpf.dataset.rf1PostbackProbe = token;
    return {
        cpf: (cpf.value || '').replace(/\D/g, ''),
        matricula: (matricula.value || '').trim(),
        orgao: (orgao.value || '').trim(),
        viewState: viewState?.value || ''
    };
}"""


_CPF_DEPENDENCIES_READY = r"""([cpfSelector, matriculaSelector, orgaoSelector,
                                  expectedCpf, token, previous]) => {
    const cpf = document.querySelector(cpfSelector);
    const matricula = document.querySelector(matriculaSelector);
    const orgao = document.querySelector(orgaoSelector);
    const viewState = document.querySelector("input[name='__VIEWSTATE']");
    if (!cpf || !matricula || !orgao) {
        return false;
    }

    const currentCpf = (cpf.value || '').replace(/\D/g, '');
    const currentMatricula = (matricula.value || '').trim();
    const currentOrgao = (orgao.value || '').trim();
    // O RF1 nem sempre preenche txtMatricula nesta etapa. Em alguns vínculos
    // a matrícula aparece somente na ficha depois de clicar em Consultar. O
    // órgão, por outro lado, é a dependência obrigatória resolvida pelo blur.
    if (currentCpf !== expectedCpf || !currentOrgao) {
        return false;
    }

    // Em um postback completo o input e o seu dataset são recriados. Em um
    // postback parcial o ViewState ou as dependências mudam. Assim valores não
    // vazios deixados pela consulta anterior nunca confirmam a nova pesquisa.
    const inputWasReplaced = cpf.dataset.rf1PostbackProbe !== token;
    const dependenciesChanged = (
        currentMatricula !== previous.matricula ||
        currentOrgao !== previous.orgao
    );
    const partialPostbackFinished = (
        (viewState?.value || '') !== previous.viewState &&
        (dependenciesChanged || previous.cpf === expectedCpf)
    );
    return inputWasReplaced || dependenciesChanged || partialPostbackFinished;
}"""


_CPF_POSTBACK_OBSERVED = r"""([cpfSelector, matriculaSelector, orgaoSelector,
                                 expectedCpf, token, previous]) => {
    const cpf = document.querySelector(cpfSelector);
    const matricula = document.querySelector(matriculaSelector);
    const orgao = document.querySelector(orgaoSelector);
    const viewState = document.querySelector("input[name='__VIEWSTATE']");
    if (!cpf) {
        return false;
    }
    const currentCpf = (cpf.value || '').replace(/\D/g, '');
    if (currentCpf !== expectedCpf) {
        return false;
    }
    return (
        cpf.dataset.rf1PostbackProbe !== token ||
        (viewState?.value || '') !== previous.viewState ||
        (matricula?.value || '').trim() !== previous.matricula ||
        (orgao?.value || '').trim() !== previous.orgao
    );
}"""


class RF1Error(RuntimeError):
    """Falha conhecida na navegação ou configuração do RF1."""


class RF1NotFound(RF1Error):
    """O portal exibiu evidência explícita de servidor inexistente."""


def _fold_text(value: object) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    return normalized.encode("ascii", "ignore").decode("ascii").casefold()


def _has_explicit_not_found(page: Page) -> bool:
    text = _fold_text(page.locator("body").inner_text())
    return any(
        evidence in text
        for evidence in (
            "servidor nao localizado",
            "servidor nao encontrado",
            "pessoa nao encontrada",
            "cpf nao encontrado",
        )
    )


def _postback_observed(
    page: Page,
    *,
    cpf_selector: str,
    matricula_selector: str,
    orgao_selector: str,
    normalized_cpf: str,
    token: str,
    previous: dict,
) -> bool:
    return bool(
        page.evaluate(
            _CPF_POSTBACK_OBSERVED,
            [
                cpf_selector,
                matricula_selector,
                orgao_selector,
                normalized_cpf,
                token,
                previous,
            ],
        )
    )


def _salvar(dados: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.stem}.tmp{path.suffix or '.xlsx'}")
    pd.DataFrame(dados).to_excel(tmp, index=False)
    tmp.replace(path)


def _digits(value: object) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _select_consignataria(page: Page, configured_value: str | None) -> None:
    selector = f"{_PFXL}ddlConsignataria"
    select = page.locator(selector)
    if select.count() != 1:
        raise RF1Error("O portal não exibiu o campo de consignatária.")
    options = select.locator("option")
    values = [option.get_attribute("value") or "" for option in options.all()]
    nonempty_values = [value for value in values if value]
    if not nonempty_values:
        # No portal de Boa Vista esse select pode permanecer sem <option> mesmo
        # com o vínculo resolvido no servidor. Nessa situação o login prossegue.
        if configured_value:
            raise RF1Error(
                "RF1_BOA_VISTA_CONSIGNATARIA foi configurada, mas o portal não "
                "ofereceu opções de consignatária após o postback."
            )
        return
    if configured_value:
        if str(configured_value) in nonempty_values:
            select.select_option(str(configured_value))
        elif len(nonempty_values) == 1:
            select.select_option(nonempty_values[0])
        else:
            raise RF1Error(
                "A consignatária configurada não está entre as opções do portal. "
                "Atualize o value da credencial RF1."
            )
    elif len(nonempty_values) == 1:
        select.select_option(nonempty_values[0])
    else:
        raise RF1Error(
            "O usuário possui mais de uma consignatária. Configure "
            "RF1_BOA_VISTA_CONSIGNATARIA com o value correto."
        )


def _login(
    page: Page,
    login_url: str,
    usuario: str,
    senha: str,
    consignataria: str | None = None,
) -> bool:
    for tentativa in range(1, LOGIN_ATTEMPTS + 1):
        page.goto(login_url, wait_until="domcontentloaded")
        user_field = page.locator(f"{_PFXL}txtUsuario")
        user_field.fill(usuario)

        # Tab dispara o onchange → postback. No RF1 atual a URL pode não mudar.
        user_field.press("Tab")
        page.wait_for_timeout(800)

        _select_consignataria(page, consignataria)

        # preenche senha DEPOIS do postback para não ser apagada
        page.fill(f"{_PFXL}txtSenha", senha)

        # Screenshot do elemento mantém o desafio e a sessão sincronizados.
        captcha_el = page.locator("img[src='Captcha.aspx']")
        captcha_el.wait_for(state="visible", timeout=10_000)
        img_bytes = captcha_el.screenshot()
        if os.getenv("CAPTCHA_DEBUG", "0") == "1":
            os.makedirs("debug_captchas", exist_ok=True)
            idx = len(os.listdir("debug_captchas"))
            with open(f"debug_captchas/captcha_login_{idx}.png", "wb") as f:
                f.write(img_bytes)
        captcha = _solve_2captcha(img_bytes)
        if not re.fullmatch(r"\d{5}", captcha):
            print(f"  [login] resposta inválida do captcha na tentativa {tentativa}.")
            continue

        page.fill(f"{_PFXL}txtValidaCaptcha", captcha)
        page.click(f"{_PFXL}btnEntrar")
        try:
            # O RF1 pode levar alguns segundos para concluir o postback e
            # redirecionar. A espera fixa de 800 ms reiniciava logins válidos.
            page.wait_for_function(
                """loginPath => !window.location.pathname
                    .toLowerCase().includes(loginPath)""",
                arg=LOGIN_PATH.lower(),
                timeout=12_000,
            )
            print("  [login] OK")
            return True
        except TimeoutError:
            pass

        print(f"  [login] falhou (tentativa {tentativa})")

    return False


def _logout(page: Page) -> bool:
    """Encerra a sessão RF1 antes de fechar o navegador.

    Fechar o Chromium não encerra a sessão no servidor RF1. Sem este passo o
    próximo login pode ser recusado como ``usuário já logado``.
    """
    if page.is_closed():
        return False
    try:
        sair = page.get_by_role("link", name="Sair", exact=True)
        if sair.count() != 1:
            return False
        sair.click()
        page.wait_for_selector(f"{_PFXL}txtUsuario", timeout=10_000)
        return True
    except (TimeoutError, PlaywrightError):
        return False


def _await_cpf_dependencies(page: Page, normalized_cpf: str) -> None:
    """Dispara e confirma o postback que resolve matrícula e órgão pelo CPF.

    O RF1 preserva os valores da consulta anterior no formulário. Por isso,
    apenas aguardar campos não vazios não prova que o novo CPF foi processado.
    Um marcador é instalado no input antes de tirar o foco: o postback completo
    recria esse elemento; o parcial altera o ViewState ou as dependências.

    Uma falha transitória recebe uma única tentativa adicional após ``reload``.
    Se o portal continuar sem confirmar a rodada, o timeout é propagado para o
    worker encerrar a sessão e reprogramar o item, sem produzir falso negativo.
    """
    cpf_selector = f"{_PFXO}txtCPF"
    matricula_selector = f"{_PFXO}txtMatricula"
    orgao_selector = f"{_PFXO}cboOrgao"
    button_selector = f"{_PFXO}btnListar"

    for attempt in range(2):
        page.wait_for_selector(button_selector, timeout=20_000)
        token = secrets.token_hex(12)
        previous = page.evaluate(
            _ARM_CPF_POSTBACK_PROBE,
            [cpf_selector, matricula_selector, orgao_selector, token],
        )
        if not isinstance(previous, dict):
            raise RF1Error(
                "O formulário RF1 não exibiu CPF, matrícula e órgão esperados."
            )

        had_not_found_evidence = _has_explicit_not_found(page)
        page.fill(cpf_selector, normalized_cpf)

        # Equivale ao gesto humano de clicar fora do CPF. O onchange do campo
        # dispara o postback ASP.NET que preenche matrícula e órgão.
        page.locator(matricula_selector).click()
        try:
            page.wait_for_function(
                _CPF_DEPENDENCIES_READY,
                arg=[
                    cpf_selector,
                    matricula_selector,
                    orgao_selector,
                    normalized_cpf,
                    token,
                    previous,
                ],
                timeout=15_000,
            )
            # Dá ao WebForms um ciclo curto para concluir handlers executados
            # imediatamente após a substituição dos controles.
            page.wait_for_timeout(250)
            return
        except TimeoutError:
            has_not_found_evidence = _has_explicit_not_found(page)
            if has_not_found_evidence and (
                not had_not_found_evidence
                or _postback_observed(
                    page,
                    cpf_selector=cpf_selector,
                    matricula_selector=matricula_selector,
                    orgao_selector=orgao_selector,
                    normalized_cpf=normalized_cpf,
                    token=token,
                    previous=previous,
                )
            ):
                raise RF1NotFound("Servidor não localizado pelo RF1.")
            if attempt:
                raise

            # Recupera postbacks que ficaram presos sem reaproveitar o DOM
            # contaminado. A segunda falha sobe como retryable_error.
            page.reload(wait_until="domcontentloaded", timeout=20_000)


def _consultar(page: Page, cpf: str) -> dict:
    normalized_cpf = _digits(cpf)
    if len(normalized_cpf) != 11:
        raise RF1Error("CPF inválido na planilha de entrada.")
    _await_cpf_dependencies(page, normalized_cpf)
    cpf_selector = f"{_PFXO}txtCPF"
    matricula_selector = f"{_PFXO}txtMatricula"
    orgao_selector = f"{_PFXO}cboOrgao"
    response_probe_token = secrets.token_hex(12)
    response_probe = page.evaluate(
        _ARM_CPF_POSTBACK_PROBE,
        [
            cpf_selector,
            matricula_selector,
            orgao_selector,
            response_probe_token,
        ],
    )
    had_not_found_evidence = _has_explicit_not_found(page)
    page.click(f"{_PFXO}btnListar")

    # O postback do WebForms é assíncrono. Um tempo fixo aqui permitia que a
    # leitura capturasse os dados visíveis da consulta anterior, duplicando
    # CPFs no resultado. Só seguimos quando o CPF devolvido é o CPF solicitado.
    returned_cpf_selector = f"{_PFXO}lblCPF"
    try:
        page.wait_for_function(
            """([selector, expected]) => {
                const element = document.querySelector(selector);
                const returned = (element?.textContent || '').replace(/\\D/g, '');
                return returned === expected;
            }""",
            arg=[returned_cpf_selector, normalized_cpf],
            timeout=15_000,
        )
    except TimeoutError:
        has_not_found_evidence = _has_explicit_not_found(page)
        if (
            isinstance(response_probe, dict)
            and has_not_found_evidence
            and (
                not had_not_found_evidence
                or _postback_observed(
                    page,
                    cpf_selector=cpf_selector,
                    matricula_selector=matricula_selector,
                    orgao_selector=orgao_selector,
                    normalized_cpf=normalized_cpf,
                    token=response_probe_token,
                    previous=response_probe,
                )
            )
        ):
            raise RF1NotFound("Servidor não localizado pelo RF1.")
        raise

    sel_nome = f"{_PFXO}lblNome"
    nome = page.locator(sel_nome)
    if nome.count() != 1 or not nome.inner_text().strip():
        raise TimeoutError("Servidor não localizado.")

    def t(sel: str) -> str:
        return page.inner_text(f"{_PFXO}{sel}").strip()

    return {
        "CPF_Retornado":      t("lblCPF"),
        "Nome":               t("lblNome"),
        "Matricula":          t("lblMatricula"),
        "Data_Nascimento":    t("lblNascimento"),
        "Margem_Beneficio":   t("lblCartaoAdianmento"),
        "Margem_Consignavel": t("lblMargemConsignavel"),
        "Margem_Reservada":   t("lblMargemReserva"),
        "Cartao_Consignado":  t("lblMargemCCredito"),
        "Regime_Trabalho":    t("lblRegimeTrabalho"),
        "Relacao_Trabalho":   t("lblRelacaoTrabalho"),
        "Categoria":          t("lblCategoria"),
        "Data_Admissao":      t("lblDataAdmissao"),
        "Situacao_Ativo":     t("lblAtivo"),
        "Prazo_Final_Vinculo": t("lblPrazoFinal"),
        "Ano_Mes_Cadastro":    t("lblAnoMesCadastro"),
        "Ano_Mes_Atualizacao": t("lblAnoMesAtualizacao"),
        "Competencia_Ferias":  t("lblCompetenciaFerias"),
        "Restricao_Renegociacao_Portabilidade": t("lblRestricaoRenCompr"),
        "Media_Margem_12_Meses": t("lblMediaMargem"),
        "Salario_Base":        t("lblValorSalarioBase"),
        "Status_Robo":        "Sucesso",
    }


def main(config: dict, input_file: Path, temp_file: Path, output_file: Path) -> None:
    login_url = config.get("url_login", DEFAULT_LOGIN_URL).strip()
    consulta_url = config.get("url_consulta", DEFAULT_QUERY_URL).strip()
    usuario = config.get("usuario", "").strip()
    senha = config.get("senha", "")
    consignataria = config.get("consignataria", "").strip() or None
    if not usuario or not senha:
        raise RF1Error("Usuário e senha do RF1 Boa Vista não configurados.")

    input_file = Path(input_file)
    temp_file = Path(temp_file)
    output_file = Path(output_file)

    df_original = pd.read_excel(input_file, dtype=str)
    coluna_cpf = next((c for c in df_original.columns if c.upper() == "CPF"), None)
    if coluna_cpf is None:
        print("Coluna 'CPF' não encontrada.")
        return
    df_original["_CPF_Normalizado"] = df_original[coluna_cpf].map(_digits)

    lista_cpfs = []
    for cpf in df_original["_CPF_Normalizado"].tolist():
        if cpf and cpf not in lista_cpfs:
            lista_cpfs.append(cpf)

    resultados: list[dict] = []
    if temp_file.exists():
        try:
            resultados = pd.read_excel(temp_file, dtype=str).to_dict("records")
        except Exception:
            resultados = []
    feitos = {_digits(r.get("CPF_Chave", "")) for r in resultados}
    pendentes = [cpf for cpf in lista_cpfs if cpf not in feitos]
    print(f"{len(resultados)} processados, {len(pendentes)} pendentes.")
    if not pendentes:
        print("Nada a processar.")
        return

    stop_flag = False
    _orig = signal.getsignal(signal.SIGINT)

    def _handle(*_):
        nonlocal stop_flag
        print("\n\nCtrl+C recebido — encerrando após o registro atual...")
        stop_flag = True

    signal.signal(signal.SIGINT, _handle)

    with sync_playwright() as pw:
        headless = os.getenv("HEADLESS", "false").lower() == "true"
        browser = pw.chromium.launch(headless=headless)
        page = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()

        try:
            if not _login(page, login_url, usuario, senha, consignataria):
                print("Login falhou após várias tentativas.")
                return

            for i, cpf in enumerate(pendentes, 1):
                if stop_flag:
                    print("Processo interrompido.")
                    break

                print(f"\n[{i}/{len(pendentes)}] CPF: {mask_cpf(cpf)}")
                try:
                    if LOGIN_PATH.lower() in page.url.lower():
                        if not _login(page, login_url, usuario, senha, consignataria):
                            raise RF1Error("Sessão expirou e o novo login falhou.")
                    if consulta_url not in page.url:
                        page.goto(consulta_url, wait_until="domcontentloaded")
                    if LOGIN_PATH.lower() in page.url.lower():
                        if not _login(page, login_url, usuario, senha, consignataria):
                            raise RF1Error("Sessão expirou ao abrir a consulta.")
                        page.goto(consulta_url, wait_until="domcontentloaded")
                    page.wait_for_selector(f"{_PFXO}btnListar")
                    try:
                        dados = _consultar(page, cpf)
                        dados["CPF_Chave"] = cpf
                        print(f"  {dados['Nome']}")
                        resultados.append(dados)
                    except TimeoutError:
                        print("  sem dados.")
                        resultados.append({"CPF_Chave": cpf, "Status_Robo": "Não Encontrado"})
                except Exception as e:
                    print(f"  ERRO [{type(e).__name__}]: {e}")
                    traceback.print_exc()
                    resultados.append({"CPF_Chave": cpf, "Status_Robo": f"Erro: {type(e).__name__}"})

                _salvar(resultados, temp_file)

            if not stop_flag:
                df_final = pd.merge(
                    df_original,
                    pd.DataFrame(resultados),
                    left_on="_CPF_Normalizado",
                    right_on="CPF_Chave",
                    how="left",
                ).drop(columns=["_CPF_Normalizado", "CPF_Chave"], errors="ignore")
                df_final.to_excel(output_file, index=False)
                if temp_file.exists():
                    temp_file.unlink()
                print(f"\n{len(resultados)} consultados → {output_file}")
            else:
                print(f"\nParcial salvo em: {temp_file}")

        except Exception as e:
            print(f"\nERRO: {e}")
        finally:
            signal.signal(signal.SIGINT, _orig)
            if not headless:
                aguardar_enter()
            browser.close()

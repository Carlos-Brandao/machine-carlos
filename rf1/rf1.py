import os
import signal
import sys
import traceback
from pathlib import Path

import pandas as pd
from playwright.sync_api import Page, sync_playwright, TimeoutError

sys.path.insert(0, str(Path(__file__).parent.parent))
from services.captcha import _solve_2captcha
from services.utils import aguardar_enter


_PFXO = "#ctl00_ctl00_ContentPlaceHolder1_ContentPlaceHolder1_"
_PFXL = "#ctl00_ContentPlaceHolder1_"


def _salvar(dados: list[dict], path: Path) -> None:
    tmp = path.with_suffix(".tmp")
    pd.DataFrame(dados).to_excel(tmp, index=False)
    tmp.replace(path)


def _login(page: Page, login_url: str, usuario: str, senha: str) -> bool:
    for tentativa in range(1, 11):
        page.wait_for_load_state("domcontentloaded")
        page.fill(f"{_PFXL}txtUsuario", usuario)

        # Tab dispara o onchange → postback popula o dropdown
        page.press(f"{_PFXL}txtUsuario", "Tab")
        page.wait_for_load_state("networkidle", timeout=10_000)

        # preenche senha DEPOIS do postback para não ser apagada
        page.fill(f"{_PFXL}txtSenha", senha)

        captcha_el = page.locator("img[src='Captcha.aspx']")
        captcha_el.wait_for(state="visible")
        img_bytes = captcha_el.screenshot()
        os.makedirs("debug_captchas", exist_ok=True)
        idx = len(os.listdir("debug_captchas"))
        with open(f"debug_captchas/captcha_login_{idx}.png", "wb") as f:
            f.write(img_bytes)
        captcha = _solve_2captcha(img_bytes)
        print(f"  [login] tentativa {tentativa}: '{captcha}'")

        page.fill(f"{_PFXL}txtValidaCaptcha", captcha)
        page.click(f"{_PFXL}btnEntrar")
        page.wait_for_load_state("domcontentloaded")

        if "ConsigAcessoUsuarioLogar" not in page.url:
            print("  [login] OK")
            return True

        print(f"  [login] falhou (tentativa {tentativa})")
        page.goto(login_url)

    return False


def _consultar(page: Page, cpf: str) -> dict:
    campo = f"{_PFXO}txtCPF"
    page.fill(campo, cpf)
    page.press(campo, "Tab")
    page.wait_for_timeout(300)
    page.click(f"{_PFXO}btnListar")

    sel_nome = f"{_PFXO}lblNome"
    page.wait_for_function(
        f"document.querySelector('{sel_nome}').innerText.trim().length > 0",
        timeout=10000,
    )

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
        "Status_Robo":        "Sucesso",
    }


def main(config: dict, input_file: Path, temp_file: Path, output_file: Path) -> None:
    login_url = config["url_login"]
    consulta_url = config["url_consulta"]
    usuario = config["usuario"]
    senha = config["senha"]

    input_file = Path(input_file)
    temp_file = Path(temp_file)
    output_file = Path(output_file)

    df_original = pd.read_excel(input_file, dtype=str)
    coluna_cpf = next((c for c in df_original.columns if c.upper() == "CPF"), None)
    if coluna_cpf is None:
        print("Coluna 'CPF' não encontrada.")
        return

    lista_cpfs = df_original[coluna_cpf].str.strip().tolist()

    resultados: list[dict] = []
    if temp_file.exists():
        try:
            resultados = pd.read_excel(temp_file, dtype=str).to_dict("records")
        except Exception:
            resultados = []
    feitos = {"".join(filter(str.isdigit, str(r.get("CPF_Chave", "")))) for r in resultados}
    pendentes = [c for c in lista_cpfs if "".join(filter(str.isdigit, c)) not in feitos]
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
        headless_mode = os.environ.get('HEADLESS', 'False').lower() == 'true'
        browser = pw.chromium.launch(headless=headless_mode)
        page = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()

        try:
            page.goto(login_url)
            if not _login(page, login_url, usuario, senha):
                print("Login falhou após várias tentativas.")
                return

            for i, cpf in enumerate(pendentes, 1):
                if stop_flag:
                    print("Processo interrompido.")
                    break

                print(f"\n[{i}/{len(pendentes)}] CPF: {cpf}")
                try:
                    if consulta_url not in page.url:
                        page.goto(consulta_url)
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
                    left_on=coluna_cpf,
                    right_on="CPF_Chave",
                    how="left",
                ).drop(columns=["CPF_Chave"], errors="ignore")
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
            aguardar_enter()

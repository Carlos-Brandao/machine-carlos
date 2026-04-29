import os
import signal
import sys
import threading
import time
import traceback
from pathlib import Path

import pandas as pd
from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).parent.parent))
from services.captcha import _solve_2captcha
from services.utils import aguardar_enter


def _salvar(dados: list[dict], path: Path) -> None:
    tmp = path.with_suffix(".tmp")
    pd.DataFrame(dados).to_excel(tmp, index=False)
    tmp.replace(path)


def _login(page: Page, login_url: str, usuario: str, senha: str) -> bool:
    for tentativa in range(1, 5):
        page.goto(login_url)
        page.wait_for_load_state("domcontentloaded")
        page.fill("#txtLogin", usuario)

        # Tab dispara onchange → postback AJAX → captcha aparece
        page.press("#txtLogin", "Tab")
        page.wait_for_load_state("networkidle", timeout=10_000)
        page.wait_for_selector("#imgCaptcha", state="visible", timeout=5_000)

        # preenche senha depois do postback terminar, antes de resolver o captcha
        page.fill("#txtSenha", senha)

        if tentativa <= 3:
            src = page.locator("#imgCaptcha").get_attribute("src")
            captcha_url = login_url.rsplit("/", 1)[0] + "/" + src.lstrip("/")
            img_bytes = page.request.get(captcha_url).body()
            os.makedirs("debug_captchas", exist_ok=True)
            idx = len(os.listdir("debug_captchas"))
            with open(f"debug_captchas/captcha_fenix_{idx}.png", "wb") as f:
                f.write(img_bytes)
            try:
                captcha = _solve_2captcha(img_bytes, regsense=1)
            except Exception as e:
                print(f"  [login] captcha erro: {e}, tentando novamente...")
                continue
            print(f"  [login] tentativa {tentativa}: '{captcha}'")
        else:
            captcha = input("  captcha manual: ").strip()

        page.fill("#txtCaptcha", captcha)
        page.click("#Entrar")
        page.wait_for_load_state("domcontentloaded")

        # login falhou se o campo de login ainda estiver visível
        if page.locator("#txtLogin").count() > 0 and page.locator("#gvOrgao").count() == 0:
            print(f"  [login] falhou (tentativa {tentativa})")
            continue

        # tela de seleção de convênio (URL permanece Login.aspx por Server.Transfer)
        if page.locator("#gvOrgao").count() > 0:
            with page.expect_navigation(wait_until="networkidle"):
                page.locator("#gvOrgao input[type='image']").first.click()

        print("  [login] OK")
        return True

    return False


def _processar(page: Page, cpf: str, matricula: str) -> dict:
    info: dict = {
        "cpf": cpf,
        "matricula": matricula,
        "nome_servidor": "N/A",
        "margem_disponivel": "0,00",
        "status": "Não Encontrado",
    }

    page.fill("#body_cpfTextBox", cpf)
    page.fill("#body_matriculaTextBox", matricula)

    with page.expect_navigation(wait_until="networkidle"):
        page.click("#body_Button1")

    erro = page.query_selector("#body_mensgemLabel")
    if not (erro and "Dados não encontrados" in erro.inner_text()):
        info["nome_servidor"] = page.input_value("#body_clienteTextBox")
        info["data_nascimento"] = page.input_value("#body_dataNascimentoTextBox")
        info["tipo_servidor"] = page.input_value("#body_categoriaTextBox")
        info["margem_disponivel"] = page.input_value("#body_margemTextBox")
        info["status"] = "OK"
        print(f"  {info['nome_servidor']} | {info['margem_disponivel']}")

    return info


def main(config: dict, input_file: Path, temp_file: Path, output_file: Path) -> None:
    login_url = config["url_login"]
    consulta_url = config["url_consulta"]
    usuario = config["usuario"]
    senha = config["senha"]

    input_file = Path(input_file)
    temp_file = Path(temp_file)
    output_file = Path(output_file)

    try:
        df = pd.read_excel(input_file, dtype=str)
        df.columns = df.columns.str.strip().str.lower()
    except Exception as e:
        print(f"Erro ao ler arquivo: {e}")
        return

    resultados: list[dict] = []
    if temp_file.exists():
        try:
            resultados = pd.read_excel(temp_file, dtype=str).to_dict("records")
        except Exception:
            resultados = []
    feitos = {
        ("".join(filter(str.isdigit, str(r.get("cpf", "")))), str(r.get("matricula", "")).strip())
        for r in resultados
    }
    df = df[~df.apply(
        lambda row: ("".join(filter(str.isdigit, str(row["cpf"]))), str(row["matricula"]).strip()) in feitos,
        axis=1,
    )].reset_index(drop=True)
    print(f"{len(resultados)} processados, {len(df)} pendentes.")
    if df.empty:
        print("Nada a processar.")
        return

    stop = threading.Event()
    _orig = signal.getsignal(signal.SIGINT)

    def _handle(*_):
        print("\n\nCtrl+C recebido — encerrando após o registro atual...")
        stop.set()

    signal.signal(signal.SIGINT, _handle)

    with sync_playwright() as p:
        page = p.chromium.launch(headless=False).new_context().new_page()
        if not _login(page, login_url, usuario, senha):
            print("Login falhou após várias tentativas.")
            return

        page.goto(consulta_url)
        page.wait_for_selector("#body_matriculaTextBox", timeout=15_000)
        print("Tela detectada. Iniciando...\n")

        try:
            for idx, row in df.iterrows():
                if stop.is_set():
                    print("Processo interrompido.")
                    break

                cpf = str(row["cpf"]).strip()
                matricula = str(row["matricula"]).strip()
                print(f"[{idx + 1}/{len(df)}] CPF: {cpf}")

                try:
                    info = _processar(page, cpf, matricula)
                    resultados.append(info)
                    _salvar(resultados, temp_file)

                    with page.expect_navigation(wait_until="networkidle"):
                        page.click("#body_voltarButton")
                    time.sleep(1)

                except Exception as e:
                    print(f"  ERRO [{type(e).__name__}]: {e}")
                    traceback.print_exc()
                    resultados.append({"cpf": cpf, "matricula": matricula, "erro": f"{type(e).__name__}: {e}"})
                    _salvar(resultados, temp_file)
                    page.goto(consulta_url)

            if not stop.is_set():
                _salvar(resultados, output_file)
                if temp_file.exists():
                    temp_file.unlink()
                print(f"\nConcluído → {output_file}")
            else:
                _salvar(resultados, temp_file)
                print(f"\nParcial salvo em: {temp_file}")

        finally:
            signal.signal(signal.SIGINT, _orig)
            aguardar_enter()

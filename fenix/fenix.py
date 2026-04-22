import signal
import sys
import threading
import time
import traceback
from pathlib import Path

import pandas as pd
from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).parent.parent))
from services.utils import aguardar_enter


def _salvar(dados: list[dict], path: Path) -> None:
    tmp = path.with_suffix(".tmp")
    pd.DataFrame(dados).to_excel(tmp, index=False)
    tmp.replace(path)


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
        info["margem_disponivel"] = page.input_value("#body_margemTextBox")
        info["status"] = "OK"
        print(f"  {info['nome_servidor']} | {info['margem_disponivel']}")

    return info


def main(config: dict, input_file: Path, temp_file: Path, output_file: Path) -> None:
    login_url = config["url_login"]
    consulta_url = config["url_consulta"]

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
        page.goto(login_url)

        print("\nFaça o login, selecione o convênio e entre na tela de Consulta de Margem.")
        print("O robô inicia assim que detectar o campo de matrícula.\n")

        page.wait_for_selector("#body_matriculaTextBox", timeout=0)
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

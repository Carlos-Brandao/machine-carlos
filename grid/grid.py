import os
import signal
import sys
import threading
import time
import traceback
from pathlib import Path

import pandas as pd
from playwright.sync_api import Page, sync_playwright, TimeoutError as PlaywrightTimeoutError

sys.path.insert(0, str(Path(__file__).parent.parent))
from services.utils import aguardar_enter, mask_cpf


def _fmt_cpf(cpf: str) -> str:
    d = "".join(filter(str.isdigit, str(cpf))).zfill(11)
    return f"{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:11]}"


def _salvar(dados: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.stem}.tmp{path.suffix or '.xlsx'}")
    pd.DataFrame(dados).to_excel(tmp, index=False)
    tmp.replace(path)


def _digitar_cpf(page: Page, selector: str, cpf: str) -> None:
    field = page.locator(selector)
    field.click()
    field.press("Control+a")
    field.press("Delete")
    time.sleep(0.1)
    for ch in cpf:
        if ch.isdigit():
            field.type(ch, delay=60)


def _aguardar_ajax(page: Page, timeout: int = 15000) -> None:
    try:
        page.wait_for_selector(
            ".ui-dialog:visible, [id$='statusDialog']:visible",
            state="hidden",
            timeout=timeout,
        )
    except Exception:
        pass
    time.sleep(0.5)


def _login(
    page: Page, url: str, usuario: str, senha: str, timeout_ms: int = 300_000
) -> None:
    page.goto(url)
    page.wait_for_load_state("networkidle")
    page.fill("#username", usuario)
    page.fill("#password", senha)
    print("Resolva o reCAPTCHA e clique em Login; o robô aguardará a navegação.")
    page.wait_for_url("**/selecaoPerfil.seam**", timeout=timeout_ms)


def _selecionar_perfil(page: Page, url_perfil: str, config: dict) -> None:
    page.goto(url_perfil)
    page.wait_for_selector(
        "#idTipoUsuarioConsignataria\\:tipoPessoaConsignataria",
        state="visible",
        timeout=30000,
    )

    page.locator("#idTipoUsuarioConsignataria\\:tipoPessoaConsignataria").select_option(
        str(config.get("tipo_pessoa", "4406"))
    )
    _aguardar_ajax(page)

    page.wait_for_selector(
        f"#idEmpresaConsignataria\\:empresaConsignataria option[value='{config.get('empresa', '7626')}']",
        state="attached",
        timeout=15000,
    )
    page.locator("#idEmpresaConsignataria\\:empresaConsignataria").select_option(
        str(config.get("empresa", "7626"))
    )
    _aguardar_ajax(page)

    page.wait_for_selector(
        f"#idPerfilConsignataria\\:perfilConsignataria option[value='{config.get('perfil', '10443')}']",
        state="attached",
        timeout=15000,
    )
    page.locator("#idPerfilConsignataria\\:perfilConsignataria").select_option(
        str(config.get("perfil", "10443"))
    )
    time.sleep(0.5)

    page.click("#btnAcessarSistema")
    page.wait_for_load_state("networkidle")


def _ir_para_margem(page: Page) -> None:
    page.locator("#submenuContrato > a").click()
    time.sleep(0.5)
    page.locator("text=Consulta de Margens").first.click()
    time.sleep(0.5)
    page.locator("text=Consultar Margem Cartão").click()
    page.wait_for_load_state("networkidle")


def _consultar(page: Page, cpf: str) -> dict | None:
    cpf_fmt = _fmt_cpf(cpf)
    print(f"CPF: {mask_cpf(cpf_fmt)}")

    _digitar_cpf(page, "#campo_cpf", cpf_fmt)
    time.sleep(0.3)
    page.click("#botaoPesquisarColaborador")
    page.wait_for_load_state("networkidle")
    time.sleep(1)

    rows = page.locator("#idTabelaColaborador_data tr")
    if rows.count() == 0 or "No records found" in (rows.first.inner_text() or ""):
        return {"cpf": cpf_fmt, "erro": "Não encontrado"}

    page.locator("#idTabelaColaborador\\:0\\:btnPesqColaborador").click()
    page.wait_for_load_state("networkidle")
    time.sleep(1)

    def t(sel: str) -> str:
        try:
            return page.locator(sel).inner_text().strip()
        except Exception:
            return ""

    dados: dict = {
        "cpf":          cpf_fmt,
        "nome":         t("td.colaboradorSelecionado:nth-child(2)"),
        "matricula":    t("td.colaboradorSelecionado:nth-child(3)"),
        "ultima_folha": t("td.colaboradorSelecionado:nth-child(4)"),
    }

    try:
        mapa: dict = {}
        for linha in page.locator(".panelGridDetalhe tr").all():
            cells = linha.locator("td").all()
            for i in range(0, len(cells) - 1, 2):
                mapa[cells[i].inner_text().strip().rstrip(":").lower()] = cells[i + 1].inner_text().strip()
        dados.update({
            "data_nascimento":    mapa.get("data nasc.", ""),
            "cargo":              mapa.get("cargo", ""),
            "lotacao":            mapa.get("lotação", ""),
            "orgao":              mapa.get("órgão", ""),
            "regime_contratacao": mapa.get("regime contratação", ""),
            "data_admissao":      mapa.get("data de admissão", ""),
            "dias_admissao":      mapa.get("dias admissão", ""),
            "obs":                mapa.get("obs", ""),
        })
    except Exception as e:
        print(f"  detalhe: {e}")

    try:
        page.locator("#idEventoRubricaVerba\\:input_idEvento").select_option("1")
        page.wait_for_load_state("networkidle")
        time.sleep(1)
    except Exception as e:
        print(f"  desconto: {e}")

    try:
        dados["margem_cartao_daycoval"] = page.locator(
            "#idMargemPositiva\\:idValorMargemDisponivel"
        ).inner_text().strip()
    except Exception:
        dados["margem_cartao_daycoval"] = ""

    print(f"  margem: {dados['margem_cartao_daycoval']}")
    return dados


def _limpar(page: Page) -> None:
    try:
        page.click("#botaoLimparColaborador")
        page.wait_for_load_state("networkidle")
        time.sleep(0.5)
    except Exception:
        pass


def main(config: dict, input_file: Path, temp_file: Path, output_file: Path) -> None:
    input_file = Path(input_file)
    temp_file = Path(temp_file)
    output_file = Path(output_file)

    df = pd.read_excel(input_file, dtype=str)
    df.columns = df.columns.str.strip().str.lower()
    col_cpf = "cpf" if "cpf" in df.columns else df.columns[0]
    cpfs = df[col_cpf].dropna().tolist()

    registros: list[dict] = []
    if temp_file.exists():
        registros = pd.read_excel(temp_file, dtype=str).to_dict("records")
    feitos = {"".join(filter(str.isdigit, str(r.get("cpf", "")))) for r in registros}
    pendentes = [c for c in cpfs if "".join(filter(str.isdigit, str(c))) not in feitos]

    print(f"{len(registros)} processados, {len(pendentes)} pendentes.")
    if not pendentes:
        return

    stop = threading.Event()
    _orig = signal.getsignal(signal.SIGINT)

    def _handle(*_):
        print("\n\nCtrl+C recebido — encerrando após o registro atual...")
        stop.set()

    signal.signal(signal.SIGINT, _handle)

    with sync_playwright() as p:
        headless = os.getenv("HEADLESS", "false").lower() == "true"
        channel = config.get("browser_channel", "").strip() or None
        launch_options = {"headless": headless}
        if channel:
            launch_options["channel"] = channel
        page = p.chromium.launch(**launch_options).new_context().new_page()

        try:
            login_timeout_ms = int(config.get("login_timeout_ms", "300000"))
            _login(
                page,
                config["url_login"],
                config["usuario"],
                config["senha"],
                login_timeout_ms,
            )
            _selecionar_perfil(page, config["url_perfil"], config)
            _ir_para_margem(page)

            for i, cpf in enumerate(pendentes, 1):
                if stop.is_set():
                    print("Processo interrompido.")
                    break

                print(f"\n[{i}/{len(pendentes)}]", end=" ")
                try:
                    r = _consultar(page, cpf)
                    if r:
                        registros.append(r)
                        _salvar(registros, temp_file)
                    _limpar(page)
                except PlaywrightTimeoutError:
                    print("timeout, pulando.")
                    registros.append({"cpf": _fmt_cpf(cpf), "erro": "Timeout"})
                    _salvar(registros, temp_file)
                    try:
                        _ir_para_margem(page)
                    except Exception:
                        pass
                except Exception as e:
                    print(f"  ERRO [{type(e).__name__}]: {e}")
                    traceback.print_exc()
                    registros.append({"cpf": _fmt_cpf(cpf), "erro": f"{type(e).__name__}: {e}"})
                    _salvar(registros, temp_file)

            if not stop.is_set():
                _salvar(registros, output_file)
                if temp_file.exists():
                    temp_file.unlink()
                print(f"\nConcluído → {output_file}")
            else:
                _salvar(registros, temp_file)
                print(f"\nParcial salvo em: {temp_file}")

        finally:
            signal.signal(signal.SIGINT, _orig)
            aguardar_enter()

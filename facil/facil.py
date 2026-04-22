import asyncio
import csv
import signal
import sys
import threading
import traceback
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from playwright.async_api import Page, async_playwright

load_dotenv(Path(__file__).parent.parent / ".env")

sys.path.insert(0, str(Path(__file__).parent.parent))
from services.captcha import resolve_captcha
from services.utils import aguardar_enter


def _carregar(arquivo: Path) -> list[tuple[str, str]]:
    df = pd.read_csv(arquivo, dtype=str) if arquivo.suffix.lower() == ".csv" \
        else pd.read_excel(arquivo, dtype=str)
    df.columns = df.columns.str.strip().str.lower()
    if "matricula" not in df.columns or "cpf" not in df.columns:
        raise ValueError(f"Colunas esperadas: 'matricula', 'cpf'. Encontradas: {list(df.columns)}")
    df = df[["matricula", "cpf"]].dropna()
    pares = list(zip(df["matricula"].str.strip(), df["cpf"].str.strip()))
    print(f"{len(pares)} registros carregados de '{arquivo}'")
    return pares


def _processados(temp_csv: Path) -> set[tuple[str, str]]:
    if not temp_csv.exists():
        return set()
    try:
        df = pd.read_csv(temp_csv, dtype=str, usecols=["_matricula", "_cpf"])
        return set(zip(df["_matricula"].str.strip(), df["_cpf"].str.strip()))
    except Exception:
        return set()


async def _login(page: Page, base_url: str, usuario: str, senha: str) -> bool:
    await page.goto(base_url + "/")
    await page.wait_for_load_state("domcontentloaded")
    await page.fill("#usuario", usuario)
    await page.fill("#senha", senha)

    print("\nResolva o captcha, clique em Entrar e pressione ENTER aqui.")
    await asyncio.to_thread(input, "ENTER: ")

    await page.wait_for_load_state("domcontentloaded")
    if "validar.php" in page.url or page.url.rstrip("/") == base_url:
        print("Login não detectado, verifique o navegador.")
        return False
    return True


async def _buscar(page: Page, base_url: str, matricula: str, cpf: str) -> bool:
    busca_url = f"{base_url}/controlador.php?pagina=busca_servidor_consignatario.php"

    await page.goto(busca_url)
    await page.wait_for_load_state("domcontentloaded")
    await page.wait_for_timeout(1500)
    await page.wait_for_selector("input[name='matricula']", timeout=15_000)

    for tentativa in range(1, 5):
        await page.fill("input[name='matricula']", matricula)
        await page.fill("input[name='cpf']", cpf)

        if tentativa <= 3:
            captcha = await resolve_captcha(page, base_url)
            print(f"  tentativa {tentativa}: '{captcha}'")
        else:
            captcha = (await asyncio.to_thread(input, "  captcha manual: ")).strip()

        await page.fill("input[name='captcha']", captcha)
        await page.click("input[type='submit'][value='Pesquisar']")
        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_timeout(800)

        if await page.locator("table.table-consig tbody tr td a").count() > 0:
            return True

        await page.goto(busca_url)
        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_timeout(1500)
        await page.wait_for_selector("input[name='matricula']", timeout=15_000)

    return False


async def _extrair(page: Page) -> dict:
    await page.locator("table.table-consig tbody tr td a").first.click()
    await page.wait_for_load_state("domcontentloaded")
    await page.wait_for_timeout(2000)

    dados: dict = {}

    for card in await page.locator("#conteudo .card").all():
        header = card.locator(".card-header")
        if await header.count() == 0:
            continue
        secao = (await header.inner_text()).strip()

        for item in await card.locator("li.list-group-item").all():
            lbl_el = item.locator("strong").first
            if await lbl_el.count() == 0:
                continue
            lbl = (await lbl_el.inner_text()).strip()
            val_el = item.locator(".text-end, .float-right").first
            val = (await val_el.inner_text()).strip() if await val_el.count() > 0 else ""
            if lbl:
                dados[f"{secao} | {lbl}"] = val

        for row in await card.locator("tr").all():
            tds = await row.locator("td").all()
            if len(tds) >= 2:
                lbl = (await tds[0].inner_text()).strip()
                val = (await tds[1].inner_text()).strip()
                if lbl:
                    dados[f"{secao} | {lbl}"] = val

    container = page.locator("#container_card_margem")
    if await container.count() > 0:
        for i, card in enumerate(await container.locator(".card").all()):
            tipo_el = card.locator("span.span_descricao_margem").first
            tipo = (await tipo_el.inner_text()).strip() if await tipo_el.count() > 0 else f"Margem {i+1}"
            paras = await card.locator("p.fs-4").all()
            for j in range(0, len(paras) - 1, 2):
                lbl = (await paras[j].inner_text()).strip()
                val = (await paras[j + 1].inner_text()).strip()
                if lbl:
                    dados[f"Margem | {tipo} | {lbl}"] = val

    return dados


async def _run(
    config: dict,
    input_file: Path,
    temp_file: Path,
    output_file: Path,
    stop: threading.Event,
) -> None:
    base_url = config["url"]
    usuario = config["usuario"]
    senha = config["senha"]

    consultas = _carregar(input_file)
    temp_csv = temp_file.with_suffix(".csv")
    feitos = _processados(temp_csv)
    pendentes = [(m, c) for m, c in consultas if (m.strip(), c.strip()) not in feitos]

    if feitos:
        print(f"{len(feitos)} já processados — {len(pendentes)} restantes.")
    if not pendentes:
        print("Nada a processar.")
        return

    csv_fh = open(temp_csv, "a", newline="", encoding="utf-8-sig")
    writer: csv.DictWriter | None = None

    def salvar(dados: dict) -> None:
        nonlocal writer
        if writer is None:
            writer = csv.DictWriter(csv_fh, fieldnames=sorted(dados.keys()), extrasaction="ignore")
            if not feitos:
                writer.writeheader()
        writer.writerow(dados)
        csv_fh.flush()

    async with async_playwright() as p:
        page = await (await (await p.chromium.launch(headless=False)).new_context()).new_page()

        try:
            if not await _login(page, base_url, usuario, senha):
                print("Login falhou.")
                csv_fh.close()
                return

            for i, (matricula, cpf) in enumerate(pendentes, 1):
                if stop.is_set():
                    print("Processo interrompido.")
                    break

                print(f"\n[{i}/{len(pendentes)}] mat={matricula} cpf={cpf}")

                try:
                    encontrado = await _buscar(page, base_url, matricula, cpf)
                    if not encontrado:
                        print("  AVISO: não encontrado após tentativas.")
                        salvar({"_matricula": matricula, "_cpf": cpf, "_erro": "não encontrado"})
                        continue

                    dados = await _extrair(page)
                    dados["_matricula"] = matricula
                    dados["_cpf"] = cpf
                    salvar(dados)
                    print(f"  {len(dados)} campos salvos.")
                except Exception as e:
                    print(f"  ERRO [{type(e).__name__}]: {e}")
                    traceback.print_exc()
                    salvar({"_matricula": matricula, "_cpf": cpf, "_erro": f"{type(e).__name__}: {e}"})

            csv_fh.close()

            if not stop.is_set():
                pd.read_csv(temp_csv, dtype=str).to_excel(output_file, index=False)
                temp_csv.unlink()
                print(f"\nConcluído → {output_file}")
            else:
                print(f"\nParcial salvo em: {temp_csv}")

        finally:
            await asyncio.to_thread(aguardar_enter)
            await page.context.browser.close()


def main(config: dict, input_file: Path, temp_file: Path, output_file: Path) -> None:
    stop = threading.Event()
    _orig = signal.getsignal(signal.SIGINT)

    def _handle(*_):
        print("\n\nCtrl+C recebido — encerrando após o registro atual...")
        stop.set()

    signal.signal(signal.SIGINT, _handle)

    try:
        asyncio.run(_run(config, Path(input_file), Path(temp_file), Path(output_file), stop))
    finally:
        signal.signal(signal.SIGINT, _orig)

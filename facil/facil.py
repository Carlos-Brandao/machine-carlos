import asyncio
import csv
import os
import signal
import sys
import threading
import traceback
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv
from playwright.async_api import Page, async_playwright

try:
    from pyngrok import ngrok
except ImportError:
    ngrok = None

load_dotenv(Path(__file__).parent.parent / ".env")

sys.path.insert(0, str(Path(__file__).parent.parent))
from services.captcha import resolve_captcha
from services.utils import aguardar_enter


# --- TELEGRAM INTEGRATION ---

def _send_telegram_message(message: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[AVISO] Telegram Token ou Chat ID ausente no .env.")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        resp = requests.post(url, json={
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "Markdown"
        }, timeout=10)
        data = resp.json()
        if not data.get("ok"):
            print(f"[ERRO] Telegram recusou o envio: {data}")
    except Exception as e:
        print(f"[ERRO] Falha ao enviar mensagem no Telegram: {e}")


async def send_telegram_message(message: str) -> None:
    await asyncio.to_thread(_send_telegram_message, message)


def _send_telegram_document(file_path: Path, caption: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[AVISO] Telegram Token ou Chat ID ausente no .env.")
        return
    url = f"https://api.telegram.org/bot{token}/sendDocument"
    try:
        with open(file_path, "rb") as f:
            resp = requests.post(url, data={
                "chat_id": chat_id,
                "caption": caption
            }, files={
                "document": f
            }, timeout=45)
        data = resp.json()
        if data.get("ok"):
            print(f"[TELEGRAM] Planilha {file_path.name} enviada com sucesso!")
        else:
            print(f"[ERRO] Falha no envio do Telegram: {data}")
    except Exception as e:
        print(f"[ERRO] Falha de rede ao enviar documento: {e}")


async def send_telegram_document(file_path: Path, caption: str) -> None:
    await asyncio.to_thread(_send_telegram_document, file_path, caption)


# --- REMOTE VNC/NGROK SESSION FALLBACK ---

remote_processes = []


def _start_remote_session() -> str | None:
    """Inicia noVNC via websockify e ngrok."""
    if not ngrok:
        print("[ERRO] pyngrok não instalado. Não é possível iniciar a sessão remota.")
        return None
    
    use_remote = os.environ.get('USE_REMOTE_LINK', 'False').lower() == 'true'
    if not use_remote:
        return None

    try:
        auth_token = os.environ.get('NGROK_AUTHTOKEN')
        if auth_token and auth_token != "COLOQUE_SEU_TOKEN_AQUI":
            ngrok.set_auth_token(auth_token)

        # Em Linux, inicia x11vnc e websockify
        if os.name != 'nt' and os.environ.get('DISPLAY'):
            print("[INFO] Iniciando x11vnc e websockify...")
            import subprocess
            p_vnc = subprocess.Popen(['x11vnc', '-display', os.environ.get('DISPLAY', ':0'), '-rfbport', '5900', '-nopw', '-listen', 'localhost', '-xkb', '-ncache', '10', '-quiet', '-forever'])
            remote_processes.append(p_vnc)
            
            p_web = subprocess.Popen(['websockify', '--web', '/usr/share/novnc', '6080', 'localhost:5900'])
            remote_processes.append(p_web)
            import time
            time.sleep(3) # Aguarda os serviços subirem
        else:
            print("[INFO] Sistema não Linux ou sem display detectado. Apenas redirecionando ngrok porta 6080 (assumindo que o serviço já roda).")
        
        # Cria túnel com retentativas para evitar erro de liberação de subdomínio (ERR_NGROK_334)
        public_url = None
        import time
        for attempt in range(10):
            try:
                public_url = ngrok.connect(6080, "http").public_url
                break
            except Exception as ex:
                print(f"[AVISO] Tentativa {attempt+1} de criar túnel falhou: {ex}. Aguardando 15s...")
                try:
                    ngrok.kill()
                except Exception:
                    pass
                time.sleep(15)
        
        if not public_url:
            raise Exception("Não foi possível estabelecer o túnel Ngrok após 10 tentativas.")

        # Adiciona os parâmetros do noVNC
        url_vnc = f"{public_url}/vnc.html?autoconnect=true&resize=remote"
        print(f"[INFO] Link Remoto criado: {url_vnc}")
        return url_vnc
    except Exception as e:
        print(f"[ERRO] Falha ao iniciar sessão remota: {e}")
        return None


async def start_remote_session() -> str | None:
    return await asyncio.to_thread(_start_remote_session)


def _stop_remote_session() -> None:
    """Encerra ngrok, websockify e x11vnc"""
    try:
        if ngrok:
            ngrok.kill()
        for p in remote_processes:
            p.terminate()
        remote_processes.clear()
        print("[INFO] Sessão remota encerrada.")
    except Exception as e:
        print(f"[ERRO] Falha ao encerrar sessão remota: {e}")


async def stop_remote_session() -> None:
    await asyncio.to_thread(_stop_remote_session)


async def check_login_success(page: Page, base_url: str) -> bool:
    """Verifica se o login foi bem sucedido."""
    # O login tem sucesso quando somos redirecionados para as páginas controladoras internas
    if "controlador.php" in page.url:
        return True
    # Verificação de elementos que só aparecem logados (menu, logout link, etc.)
    try:
        if await page.locator("a[href*='logout']").count() > 0 or await page.locator("a[href*='sair']").count() > 0:
            return True
    except Exception:
        pass
    return False


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


async def _fechar_modais(page: Page) -> None:
    for sel in ("button.btn-close", "button[data-coreui-dismiss='modal']", "button[data-dismiss='modal']"):
        for btn in await page.locator(sel).all():
            try:
                if await btn.is_visible():
                    await btn.click()
                    await page.wait_for_timeout(300)
            except Exception:
                pass


async def _login(page: Page, base_url: str, usuario: str, senha: str) -> bool:
    # 1. Tentativas Automáticas
    for tentativa in range(1, 4):
        await page.goto(base_url + "/")
        await page.wait_for_load_state("domcontentloaded")
        await page.fill("#usuario", usuario)
        await page.fill("#senha", senha)

        try:
            captcha = await resolve_captcha(page, base_url)
            print(f"  [login] tentativa {tentativa}: '{captcha}'")
            await page.fill("input[name='captcha']", captcha)
            await page.click("button[type='submit']")
            await page.wait_for_load_state("domcontentloaded")
            await page.wait_for_timeout(2000)

            if await check_login_success(page, base_url):
                print("  [login] OK")
                await send_telegram_message("✅ *Login Automático com Sucesso:* Captcha resolvido e login efetuado.")
                return True
        except Exception as e:
            print(f"  [login] erro na tentativa {tentativa}: {e}")

        print(f"  [login] falhou (tentativa {tentativa})")

    # 2. Fallback de Login Manual / Remoto
    print("[AVISO] Login automático falhou. Iniciando fallback...")
    await send_telegram_message("⚠️ *Falha no Login Automático:* Não foi possível logar via 2Captcha. Iniciando fallback de login manual...")

    link_remoto = await start_remote_session()
    if link_remoto:
        await send_telegram_message(
            f"⚠️ *Login Manual Necessário!*\n"
            f"Acesse o link abaixo para resolver o captcha e clicar em Entrar:\n\n"
            f"{link_remoto}\n\n"
            f"*Atenção:* Aguardando você acessar..."
        )
    else:
        await send_telegram_message(
            "⚠️ *Login Manual Necessário!*\n"
            "O link remoto não pôde ser criado. Por favor, realize o login diretamente no navegador aberto na máquina local."
        )

    # Loop aguardando o usuário fazer login (120 * 5s = 10 minutos)
    for _ in range(120):
        if await check_login_success(page, base_url):
            print("[INFO] Login detectado!")
            await send_telegram_message("✅ *Login efetuado com sucesso!* Retomando o robô...")
            await stop_remote_session()
            return True
        await page.wait_for_timeout(5000)

    print("[ERRO] Tempo limite para login manual esgotado.")
    await send_telegram_message("❌ *Tempo esgotado:* O login manual não foi concluído em 10 minutos.")
    await stop_remote_session()
    return False


async def _buscar(page: Page, base_url: str, matricula: str, cpf: str) -> bool:
    busca_url = f"{base_url}/controlador.php?pagina=busca_servidor_consignatario.php"

    await page.goto(busca_url)
    await page.wait_for_load_state("domcontentloaded")
    await page.wait_for_timeout(1500)
    await _fechar_modais(page)
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
        await _fechar_modais(page)
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
    convenio = config.get("convenio", "paulista")

    consultas = _carregar(input_file)
    temp_csv = temp_file.with_suffix(".csv")
    feitos = _processados(temp_csv)
    pendentes = [(m, c) for m, c in consultas if (m.strip(), c.strip()) not in feitos]

    if feitos:
        print(f"{len(feitos)} já processados — {len(pendentes)} restantes.")
    if not pendentes:
        print("Nada a processar.")
        await send_telegram_message(
            f"ℹ️ *Bot Fácil:* Nada a processar para o convênio `{convenio}`. Todos os registros já estão processados."
        )
        return

    # Mensagem de início
    await send_telegram_message(
        f"🚀 *Robô FÁCIL Iniciado!*\n"
        f"📂 *Arquivo:* `{input_file.name}`\n"
        f"🏛️ *Convênio:* `{convenio}`\n"
        f"📊 *Total do Lote:* {len(consultas)} registros\n"
        f"🔄 *A Processar:* {len(pendentes)} CPFs\n"
        f"✅ *Já finalizados:* {len(feitos)}"
    )

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

    headless_mode = os.getenv("HEADLESS", "False").lower() == "true"

    async with async_playwright() as p:
        page = await (await (await p.chromium.launch(headless=headless_mode)).new_context()).new_page()

        # Ocultação da propriedade primária de automação
        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        """)

        try:
            if not await _login(page, base_url, usuario, senha):
                print("Login falhou.")
                await send_telegram_message("❌ *Falha Crítica no Login:* O robô não conseguiu acessar o painel do sistema.")
                csv_fh.close()
                return

            for i, (matricula, cpf) in enumerate(pendentes, 1):
                # Verificar janela de horário (07:00 às 21:00 BRT)
                import datetime
                agora_utc = datetime.datetime.now(datetime.timezone.utc)
                agora_br = agora_utc - datetime.timedelta(hours=3)
                if agora_br.hour >= 21 or agora_br.hour < 7:
                    msg_pause = (
                        f"⏳ *Horário Limite Atingido ({agora_br.strftime('%H:%M')} BRT)*\n"
                        f"A execução do Bot Fácil foi pausada com segurança pois está fora da janela programada (07:00 às 21:00).\n"
                        f"Progresso atual: {i-1}/{len(pendentes)} consultados nesta rodada.\n"
                        f"O robô salvará o estado atual e retomará no próximo ciclo."
                    )
                    print(f"[INFO] {msg_pause}")
                    await send_telegram_message(msg_pause)
                    break

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

                # Report de progresso no Telegram a cada 50 registros
                if i % 50 == 0:
                    await send_telegram_message(
                        f"📈 *Status do Robô FÁCIL:*\n"
                        f"*Progresso:* {i}/{len(pendentes)} CPFs consultados nesta rodada."
                    )

            csv_fh.close()

            if not stop.is_set():
                pd.read_csv(temp_csv, dtype=str).to_excel(output_file, index=False)
                temp_csv.unlink()
                print(f"\nConcluído → {output_file}")

                await send_telegram_message("✅ *Processamento Concluído com Sucesso pelo Bot Fácil!*")
                await send_telegram_document(
                    output_file,
                    f"📊 *Resultados de Servidores Encontrados — {convenio.upper()}*\n"
                    f"- Total processado: {len(pendentes)} registros"
                )
            else:
                print(f"\nParcial salvo em: {temp_csv}")
                await send_telegram_message(
                    f"⏳ *Execução Pausada (Ctrl+C):*\n"
                    f"O progresso parcial foi salvo localmente em `temp/`. O robô poderá retomar deste ponto na próxima execução."
                )

        except Exception as e:
            print(f"[ERRO] Erro crítico na execução: {e}")
            await send_telegram_message(f"🚨 *Erro Crítico no Robô FÁCIL:* {e}")
        finally:
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

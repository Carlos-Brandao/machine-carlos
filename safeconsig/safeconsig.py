import os
import time
import signal
import sys
import datetime
import traceback
import threading
import subprocess
from pathlib import Path
import pandas as pd
import requests
from playwright.sync_api import sync_playwright, Page, TimeoutError as PlaywrightTimeoutError

try:
    from pyngrok import ngrok
except ImportError:
    ngrok = None

# --- TELEGRAM INTEGRATION ---

def send_telegram_message(message: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        requests.post(url, json={
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "Markdown"
        }, timeout=10)
    except Exception as e:
        print(f"[ERRO] Falha ao enviar Telegram: {e}")

def send_telegram_document(file_path: Path, caption: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendDocument"
    try:
        with open(file_path, "rb") as f:
            requests.post(url, data={
                "chat_id": chat_id,
                "caption": caption
            }, files={
                "document": f
            }, timeout=45)
    except Exception as e:
        print(f"[ERRO] Falha ao enviar documento Telegram: {e}")

# --- REMOTE VNC/NGROK SESSION FALLBACK ---

remote_processes = []

def start_remote_session() -> str | None:
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
        
        print("[VNC] Iniciando x11vnc...")
        vnc_proc = subprocess.Popen(
            ["x11vnc", "-display", ":99", "-forever", "-shared", "-nopw", "-listen", "localhost", "-rfbport", "5900"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        remote_processes.append(vnc_proc)
        
        print("[VNC] Iniciando websockify...")
        web_proc = subprocess.Popen(
            ["websockify", "--web", "/usr/share/novnc", "6080", "localhost:5900"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        remote_processes.append(web_proc)
        
        print("[VNC] Abrindo túnel Ngrok...")
        public_url = None
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

        url_vnc = f"{public_url}/vnc.html?autoconnect=true&resize=remote"
        print(f"[INFO] Link Remoto criado: {url_vnc}")
        return url_vnc
        
    except Exception as e:
        print(f"[ERRO] Falha ao iniciar sessão remota: {e}")
        stop_remote_session()
        return None

def stop_remote_session() -> None:
    """Encerra todos os processos de acesso remoto e fecha túneis ngrok."""
    print("[VNC] Encerrando processos de acesso remoto...")
    for proc in remote_processes:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    remote_processes.clear()
    
    if ngrok:
        try:
            ngrok.disconnect(None)
            ngrok.kill()
        except Exception:
            pass

# --- CAPTCHA RESOLUTION ---

def solve_turnstile(api_key: str, sitekey: str, pageurl: str) -> str | None:
    """Resolve o Cloudflare Turnstile usando a API do 2Captcha."""
    print(f"[CAPTCHA] Enviando Turnstile para o 2Captcha (sitekey={sitekey})...")
    url_in = "http://2captcha.com/in.php"
    payload = {
        'key': api_key,
        'method': 'turnstile',
        'sitekey': sitekey,
        'pageurl': pageurl,
        'json': 1
    }
    
    try:
        resp = requests.post(url_in, data=payload, timeout=15)
        res = resp.json()
        if res.get('status') != 1:
            print(f"[CAPTCHA] Erro ao enviar Turnstile: {res}")
            return None
        request_id = res.get('request')
    except Exception as e:
        print(f"[CAPTCHA] Falha na comunicação com o 2Captcha: {e}")
        return None

    url_res = "http://2captcha.com/res.php"
    params = {
        'key': api_key,
        'action': 'get',
        'id': request_id,
        'json': 1
    }
    
    print("[CAPTCHA] Aguardando resolução do Turnstile...")
    for _ in range(30):
        time.sleep(5)
        try:
            resp = requests.get(url_res, params=params, timeout=10)
            res = resp.json()
            if res.get('status') == 1:
                token = res.get('request')
                print("[CAPTCHA] Turnstile resolvido com sucesso!")
                return token
            elif res.get('request') == 'CAPCHA_NOT_READY':
                continue
            else:
                print(f"[CAPTCHA] Resposta inesperada do 2Captcha: {res}")
                return None
        except Exception as e:
            print(f"[CAPTCHA] Falha ao consultar resultado do captcha: {e}")
            
    print("[CAPTCHA] Timeout de 150 segundos excedido.")
    return None

def check_login_success(page: Page) -> bool:
    """Verifica se o login foi bem sucedido através de múltiplos indicadores."""
    indicadores = [
        'span:has-text("Painel")',
        'button:has-text("Nova Consulta de Margem")',
        'text="Informações de Contato"',
        'text="INFORMAÇÕES DE CONTATO"',
        'text="Consultas"',
        'text="CONSULTAS"',
        'text="Início"',
        'text="INÍCIO"'
    ]
    for ind in indicadores:
        try:
            if page.locator(ind).first.is_visible():
                print(f"[INFO] Login confirmado por indicador: {ind}")
                return True
        except Exception:
            pass
    return False

# --- MAIN BOT RUNNER ---

def run(config: dict, input_file: Path, temp_file: Path, output_file: Path, stop: threading.Event) -> None:
    login_url = config["url_login"]
    consulta_url = config["url_consulta"]
    usuario = config["usuario"]
    senha = config["senha"]
    convenio = config.get("convenio", "fortaleza").lower()

    # Se temp_file já existe, retomamos dele; senão, copiamos o input_file para temp_file
    if temp_file.exists():
        print(f"[INFO] Carregando progresso anterior de {temp_file}")
        df = pd.read_excel(temp_file, dtype=str)
    else:
        print(f"[INFO] Iniciando novo processamento. Copiando de {input_file}")
        df = pd.read_excel(input_file, dtype=str)
        # Ajuste de Colunas se necessário
        if 'Margem' in df.columns:
            df = df.rename(columns={'Margem': 'Margem Emprestimo'})
            
        colunas_extracao = ['Matricula', 'Margem Emprestimo', 'Margem Beneficio', 'Vinculo', 'Secretaria', 'Cargo']
        for col in colunas_extracao:
            if col not in df.columns:
                df[col] = None
        df.to_excel(temp_file, index=False)

    if 'CPF' not in df.columns:
        print("[ERRO] A planilha de entrada precisa ter a coluna 'CPF'.")
        return

    # Identifica CPFs já processados
    processed_cpfs = set()
    for _, row in df.iterrows():
        m_emp = str(row.get('Margem Emprestimo', '')).strip()
        m_ben = str(row.get('Margem Beneficio', '')).strip()
        
        valid_emp = m_emp not in ['', 'None', 'nan', 'ERRO', 'TIMEOUT']
        valid_ben = m_ben not in ['', 'None', 'nan', 'ERRO', 'TIMEOUT']
        
        if valid_emp or valid_ben:
            processed_cpfs.add(row['CPF'])

    unique_cpfs = df['CPF'].unique()
    unprocessed_cpfs = [cpf for cpf in unique_cpfs if cpf not in processed_cpfs]
    total_cpfs_to_process = len(unprocessed_cpfs)

    print(f'[INFO] Convênio: {convenio.upper()}')
    print(f'[INFO] CPFs já processados: {len(processed_cpfs)}')
    print(f'[INFO] CPFs a processar: {total_cpfs_to_process}')
    send_telegram_message(
        f"🚀 *Robô SafeConsig ({convenio.upper()}) Iniciado!*\n"
        f"- Total do Lote: {len(unique_cpfs)} CPFs\n"
        f"- Já processados: {len(processed_cpfs)}\n"
        f"- A processar: {total_cpfs_to_process}"
    )

    if total_cpfs_to_process == 0:
        print("[INFO] Nada a processar.")
        # Se concluiu tudo, salva no output_file e remove temp_file
        df.to_excel(output_file, index=False)
        if temp_file.exists():
            temp_file.unlink()
        return

    profile_dir = Path(__file__).parent.parent / "chrome_profile"
    headless_mode = os.environ.get('HEADLESS', 'False').lower() == 'true'

    tabela_selector = 'tbody[id="tabView:pesquisaMutuario:listaColaborador:input_data"]'
    detalhes_selector = 'div.grid-colaborador'

    with sync_playwright() as p:
        try:
            print(f'[INFO] Inicializando navegador Chrome (Headless={headless_mode})...')
            try:
                context = p.chromium.launch_persistent_context(
                    user_data_dir=str(profile_dir),
                    headless=headless_mode,
                    channel="chrome",
                    viewport={"width": 1280, "height": 720},
                    args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-gpu", "--window-position=0,0", "--window-size=1280,720"]
                )
            except Exception:
                print('[AVISO] Chrome não encontrado, usando Chromium padrão...')
                context = p.chromium.launch_persistent_context(
                    user_data_dir=str(profile_dir),
                    headless=headless_mode,
                    viewport={"width": 1280, "height": 720},
                    args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-gpu", "--window-position=0,0", "--window-size=1280,720"]
                )
            
            page = context.pages[0]
            page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
            
        except Exception as e:
            print(f'[ERRO] Falha ao inicializar o motor de navegação: {e}')
            send_telegram_message(f"❌ *Erro Crítico de Inicialização:* {e}")
            return

        try:
            print('[INFO] Acessando tela de login...')
            page.goto(login_url, timeout=30000)
            
            if page.locator('[id="idLogin"]').is_visible():
                page.fill('[id="idLogin"]', usuario)
                page.fill('[id="senhaUsuario"]', senha)

            # Lógica para quebra de captcha no login
            turnstile_container = page.locator('.cf-turnstile').first
            if turnstile_container.is_visible():
                sitekey = turnstile_container.get_attribute('data-sitekey')
                print(f'[CAPTCHA] Turnstile detectado. Sitekey: {sitekey}')
                
                api_key = os.environ.get('TWOCAPTCHA_API_KEY')
                if api_key:
                    send_telegram_message("🔄 *Captcha detectado.* Resolvendo automaticamente...")
                    token = solve_turnstile(api_key, sitekey, page.url)

                    if token:
                        print('[CAPTCHA] Injetando token de resposta...')
                        page.evaluate(f"""() => {{
                            let form = document.querySelector('form') || document.forms[0];
                            if (form) {{
                                let input = document.getElementsByName('cf-turnstile-response')[0];
                                if (!input) {{
                                    input = document.createElement('input');
                                    input.type = 'hidden';
                                    input.name = 'cf-turnstile-response';
                                    form.appendChild(input);
                                }}
                                input.value = '{token}';
                                
                                let input2 = document.getElementsByName('g-recaptcha-response')[0];
                                if (!input2) {{
                                    input2 = document.createElement('input');
                                    input2.type = 'hidden';
                                    input2.name = 'g-recaptcha-response';
                                    form.appendChild(input2);
                                }}
                                input2.value = '{token}';
                            }}
                        }}""")
                        
                        page.wait_for_timeout(1000)
                        page.click('button[type="submit"]')
                        page.wait_for_timeout(3000)
                    else:
                        send_telegram_message("⚠️ *Falha na resolução do captcha.* Por favor resolva manualmente.")

            # Verifica se o login teve sucesso
            page.wait_for_timeout(3000)
            if not check_login_success(page):
                print('[ERRO] Login automático via 2Captcha falhou.')
                
                # Se for antes das 09h BRT (ex: execução das 06h), encerra silenciosamente
                agora_utc = datetime.datetime.now(datetime.timezone.utc)
                agora_br = agora_utc - datetime.timedelta(hours=3)
                if agora_br.hour < 9 and os.environ.get('MANUAL_RUN') != 'True':
                    print("[INFO] Falha no login antes das 09:00 BRT. Encerrando execução sem fallback.")
                    send_telegram_message(f"⚠️ *Falha no Login Automático (07:00):* Não foi possível logar de forma automática via 2Captcha no convênio {convenio.upper()}. O robô foi encerrado.")
                    return
                
                # Tenta Sessão Remota (Link Remoto VNC)
                link_remoto = start_remote_session()
                if link_remoto:
                    send_telegram_message(f"⚠️ *Falha no Login Automático!*\nO robô precisa de ajuda.\nAcesse o link abaixo para resolver o captcha e entrar:\n\n{link_remoto}")
                    print("[INFO] Aguardando login manual pelo link remoto...")
                    
                    for _ in range(120): # 10 minutos
                        if check_login_success(page):
                            print("[INFO] Login detectado via sessão remota!")
                            send_telegram_message("✅ *Login efetuado com sucesso!* Retomando o robô...")
                            break
                        page.wait_for_timeout(5000)
                    else:
                        print("[ERRO] Tempo limite para login manual esgotado.")
                        send_telegram_message("❌ *Tempo esgotado:* Ninguém resolveu o Captcha pelo link.")
                        stop_remote_session()
                        return
                    stop_remote_session()
                else:
                    send_telegram_message("❌ *Falha no Login Automático:* O robô não conseguiu acessar e o Link Remoto não está ativado.")
                    return

            print('[INFO] Login efetuado com sucesso!')
            send_telegram_message(f"✅ *Login com Sucesso:* Conectado ao SafeConsig ({convenio.upper()}).")

            colunas_extracao = ['Matricula', 'Margem Emprestimo', 'Margem Beneficio', 'Vinculo', 'Secretaria', 'Cargo']

            for idx_cpf, cpf in enumerate(unprocessed_cpfs):
                # Janela SafeConsig: dias úteis, 07h às 18h BRT.
                agora_utc = datetime.datetime.now(datetime.timezone.utc)
                agora_br = agora_utc - datetime.timedelta(hours=3)
                if agora_br.weekday() >= 5 or agora_br.hour >= 18 or agora_br.hour < 7:
                    msg_pause = (
                        f"⏳ *Horário Limite Atingido ({agora_br.strftime('%H:%M')})*\n"
                        f"A execução foi pausada de forma segura e resumirá no próximo ciclo.\n"
                        f"- CPFs consultados hoje: {idx_cpf}"
                    )
                    print(f"[INFO] {msg_pause}")
                    send_telegram_message(msg_pause)
                    send_telegram_document(
                        temp_file, 
                        f"📊 *Progresso Parcial ({convenio.upper()} - {agora_br.strftime('%H:%M')})*"
                    )
                    break

                if stop.is_set():
                    print("[INFO] Interrupção (Ctrl+C) detectada.")
                    send_telegram_message(f"⏳ *Execução Pausada:* O robô {convenio.upper()} foi interrompido e retomará de onde parou.")
                    break

                raw_cpf = str(cpf).strip().split('.')[0].split('-')[0]
                cpf_padded = raw_cpf.zfill(11)

                print(f'[{idx_cpf + 1}/{total_cpfs_to_process}] Pesquisando CPF: {cpf_padded}')

                try:
                    results_emprestimo = {}
                    results_beneficio = {}
                    other_fields = {}

                    # --- CONSULTA DE EMPRÉSTIMO ---
                    page.goto(consulta_url, timeout=20000)
                    page.wait_for_timeout(1000)

                    # Se for Maranguape, precisa selecionar a opção 498
                    if convenio == "maranguape":
                        label_text = page.locator('span[id="tabView:pesquisaMutuario:testandooid:input_label"]').inner_text().strip()
                        if "498" not in label_text:
                            page.click('div[id="tabView:pesquisaMutuario:testandooid:input"] .ui-selectonemenu-trigger')
                            page.wait_for_selector('div[id="tabView:pesquisaMutuario:testandooid:input_panel"]', state='visible')
                            with page.expect_response(lambda r: "consulta/margem" in r.url, timeout=20000):
                                page.locator('li.ui-selectonemenu-item:has-text("498 EMP SOMAPAY EMPRESTIMO")').click()
                            page.wait_for_timeout(1000)

                    # Preenche o CPF
                    page.locator('[id="tabView:pesquisaMutuario:j_idt414:j_idt416"]').clear()
                    page.fill('[id="tabView:pesquisaMutuario:j_idt414:j_idt416"]', cpf_padded)
                    with page.expect_response(lambda r: "consulta/margem" in r.url, timeout=20000):
                        page.click('[id="tabView:pesquisaMutuario:j_idt420"]')
                    page.wait_for_timeout(1500)

                    page.wait_for_selector(f'{tabela_selector}, {detalhes_selector}', timeout=15000)

                    if page.locator(detalhes_selector).is_visible():
                        matricula = page.locator('div.ui-grid-row:has(span:has-text("Matrícula:")) .ui-grid-col-3').inner_text().strip()
                        elementos_col5 = page.locator('div.grid-colaborador .ui-grid-col-5').all_inner_texts()
                        vinculo = elementos_col5[0].strip() if len(elementos_col5) > 0 else 'N/A'
                        secretaria = elementos_col5[1].strip() if len(elementos_col5) > 1 else 'N/A'
                        cargo = elementos_col5[3].strip() if len(elementos_col5) > 3 else 'N/A'
                        margem_td = page.locator('tr:has(span:has-text("Margem Líquida (Valor Disponível):")) td').nth(1)
                        margem = margem_td.inner_text().strip()
                        results_emprestimo[matricula] = margem
                        other_fields[matricula] = (vinculo, secretaria, cargo)
                    else:
                        rows = page.locator(f'{tabela_selector} tr')
                        row_count = rows.count()
                        if not (row_count == 1 and "Nenhum registro" in rows.first.inner_text()):
                            for i in range(row_count):
                                if i > 0:
                                    btn_nova_consulta = page.locator('button:has-text("Nova Consulta de Margem")')
                                    if btn_nova_consulta.is_visible():
                                        with page.expect_response(lambda r: "consulta/margem" in r.url, timeout=20000):
                                            btn_nova_consulta.click()
                                    else:
                                        page.goto(consulta_url, timeout=20000)
                                    page.wait_for_timeout(1000)

                                    if convenio == "maranguape":
                                        page.click('div[id="tabView:pesquisaMutuario:testandooid:input"] .ui-selectonemenu-trigger')
                                        page.wait_for_selector('div[id="tabView:pesquisaMutuario:testandooid:input_panel"]', state='visible')
                                        with page.expect_response(lambda r: "consulta/margem" in r.url, timeout=20000):
                                            page.locator('li.ui-selectonemenu-item:has-text("498 EMP SOMAPAY EMPRESTIMO")').click()
                                        page.wait_for_timeout(1000)

                                    page.locator('[id="tabView:pesquisaMutuario:j_idt414:j_idt416"]').clear()
                                    page.fill('[id="tabView:pesquisaMutuario:j_idt414:j_idt416"]', cpf_padded)
                                    with page.expect_response(lambda r: "consulta/margem" in r.url, timeout=20000):
                                        page.click('[id="tabView:pesquisaMutuario:j_idt420"]')
                                    page.wait_for_timeout(1500)
                                    page.wait_for_selector(tabela_selector, timeout=15000)

                                row = page.locator(f'{tabela_selector} tr').nth(i)
                                matricula_label = row.locator('td').nth(2).locator('.ui-outputlabel-label')
                                matricula = matricula_label.inner_text().strip()
                                
                                btn = row.locator('button')
                                btn.click()
                                page.locator(f'div.grid-colaborador:has-text("{matricula}")').wait_for(timeout=12000)
                                
                                elementos_col5 = page.locator('div.grid-colaborador .ui-grid-col-5').all_inner_texts()
                                vinculo = elementos_col5[0].strip() if len(elementos_col5) > 0 else 'N/A'
                                secretaria = elementos_col5[1].strip() if len(elementos_col5) > 1 else 'N/A'
                                cargo = elementos_col5[3].strip() if len(elementos_col5) > 3 else 'N/A'
                                margem_td = page.locator('tr:has(span:has-text("Margem Líquida (Valor Disponível):")) td').nth(1)
                                margem = margem_td.inner_text().strip()
                                
                                results_emprestimo[matricula] = margem
                                other_fields[matricula] = (vinculo, secretaria, cargo)

                    # --- CONSULTA DE BENEFÍCIO (Apenas Maranguape) ---
                    if convenio == "maranguape":
                        btn_nova_consulta = page.locator('button:has-text("Nova Consulta de Margem")')
                        if btn_nova_consulta.is_visible():
                            with page.expect_response(lambda r: "consulta/margem" in r.url, timeout=20000):
                                btn_nova_consulta.click()
                        else:
                            page.goto(consulta_url, timeout=20000)
                        page.wait_for_timeout(1000)

                        label_text = page.locator('span[id="tabView:pesquisaMutuario:testandooid:input_label"]').inner_text().strip()
                        if "499" not in label_text:
                            page.click('div[id="tabView:pesquisaMutuario:testandooid:input"] .ui-selectonemenu-trigger')
                            page.wait_for_selector('div[id="tabView:pesquisaMutuario:testandooid:input_panel"]', state='visible')
                            with page.expect_response(lambda r: "consulta/margem" in r.url, timeout=20000):
                                page.locator('li.ui-selectonemenu-item:has-text("499 ADIANTA SOMAPAY CARTAO CONSIGNADO")').click()
                            page.wait_for_timeout(1000)

                        page.locator('[id="tabView:pesquisaMutuario:j_idt414:j_idt416"]').clear()
                        page.fill('[id="tabView:pesquisaMutuario:j_idt414:j_idt416"]', cpf_padded)
                        with page.expect_response(lambda r: "consulta/margem" in r.url, timeout=20000):
                            page.click('[id="tabView:pesquisaMutuario:j_idt420"]')
                        page.wait_for_timeout(1500)

                        page.wait_for_selector(f'{tabela_selector}, {detalhes_selector}', timeout=15000)

                        if page.locator(detalhes_selector).is_visible():
                            matricula = page.locator('div.ui-grid-row:has(span:has-text("Matrícula:")) .ui-grid-col-3').inner_text().strip()
                            margem_td = page.locator('tr:has(span:has-text("Margem Líquida (Valor Disponível):")) td').nth(1)
                            margem = margem_td.inner_text().strip()
                            results_beneficio[matricula] = margem
                        else:
                            rows = page.locator(f'{tabela_selector} tr')
                            row_count = rows.count()
                            if not (row_count == 1 and "Nenhum registro" in rows.first.inner_text()):
                                for i in range(row_count):
                                    if i > 0:
                                        btn_nova_consulta = page.locator('button:has-text("Nova Consulta de Margem")')
                                        if btn_nova_consulta.is_visible():
                                            with page.expect_response(lambda r: "consulta/margem" in r.url, timeout=20000):
                                                btn_nova_consulta.click()
                                        else:
                                            page.goto(consulta_url, timeout=20000)
                                        page.wait_for_timeout(1000)

                                        page.click('div[id="tabView:pesquisaMutuario:testandooid:input"] .ui-selectonemenu-trigger')
                                        page.wait_for_selector('div[id="tabView:pesquisaMutuario:testandooid:input_panel"]', state='visible')
                                        with page.expect_response(lambda r: "consulta/margem" in r.url, timeout=20000):
                                            page.locator('li.ui-selectonemenu-item:has-text("499 ADIANTA SOMAPAY CARTAO CONSIGNADO")').click()
                                        page.wait_for_timeout(1000)

                                        page.locator('[id="tabView:pesquisaMutuario:j_idt414:j_idt416"]').clear()
                                        page.fill('[id="tabView:pesquisaMutuario:j_idt414:j_idt416"]', cpf_padded)
                                        with page.expect_response(lambda r: "consulta/margem" in r.url, timeout=20000):
                                            page.click('[id="tabView:pesquisaMutuario:j_idt420"]')
                                        page.wait_for_timeout(1500)
                                        page.wait_for_selector(tabela_selector, timeout=15000)

                                    row = page.locator(f'{tabela_selector} tr').nth(i)
                                    matricula_label = row.locator('td').nth(2).locator('.ui-outputlabel-label')
                                    matricula = matricula_label.inner_text().strip()
                                    
                                    btn = row.locator('button')
                                    btn.click()
                                    page.locator(f'div.grid-colaborador:has-text("{matricula}")').wait_for(timeout=12000)
                                    
                                    margem_td = page.locator('tr:has(span:has-text("Margem Líquida (Valor Disponível):")) td').nth(1)
                                    margem = margem_td.inner_text().strip()
                                    results_beneficio[matricula] = margem

                    # Une os resultados de empréstimo e benefício
                    all_scraped_mats = set(list(results_emprestimo.keys()) + list(results_beneficio.keys()))
                    results = []
                    for mat in all_scraped_mats:
                        m_emp = results_emprestimo.get(mat, 'NÃO ENCONTRADO')
                        m_ben = results_beneficio.get(mat, 'NÃO ENCONTRADO')
                        vin, sec, car = other_fields.get(mat, ('N/A', 'N/A', 'N/A'))
                        results.append((mat, m_emp, m_ben, vin, sec, car))

                    # Atualiza o DataFrame
                    for res in results:
                        matching_rows = df[(df['CPF'] == cpf) & (df['Matricula'] == res[0])]
                        if not matching_rows.empty:
                            idx = matching_rows.index[0]
                            df.at[idx, 'Margem Emprestimo'] = res[1]
                            df.at[idx, 'Margem Beneficio'] = res[2]
                            df.at[idx, 'Vinculo'] = res[3]
                            df.at[idx, 'Secretaria'] = res[4]
                            df.at[idx, 'Cargo'] = res[5]
                        else:
                            # Tenta preencher alguma linha genérica vazia deste CPF
                            generic_rows = df[(df['CPF'] == cpf) & (df['Matricula'].isna() | (df['Matricula'].astype(str).str.strip() == '') | (df['Matricula'] == 'NÃO ENCONTRADO'))]
                            if not generic_rows.empty:
                                idx = generic_rows.index[0]
                                df.at[idx, 'Matricula'] = res[0]
                                df.at[idx, 'Margem Emprestimo'] = res[1]
                                df.at[idx, 'Margem Beneficio'] = res[2]
                                df.at[idx, 'Vinculo'] = res[3]
                                df.at[idx, 'Secretaria'] = res[4]
                                df.at[idx, 'Cargo'] = res[5]
                            else:
                                # Senão, insere nova linha
                                new_row = {
                                    'CPF': cpf,
                                    'Matricula': res[0],
                                    'Margem Emprestimo': res[1],
                                    'Margem Beneficio': res[2],
                                    'Vinculo': res[3],
                                    'Secretaria': res[4],
                                    'Cargo': res[5]
                                }
                                df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
                                
                    # Marca demais linhas remanescentes para o CPF como NÃO ENCONTRADO
                    remaining_rows = df[(df['CPF'] == cpf) & (~df['Matricula'].isin(all_scraped_mats))]
                    for idx in remaining_rows.index:
                        for col in colunas_extracao:
                            df.at[idx, col] = 'NÃO ENCONTRADO'

                    print(f'[SUCESSO] CPF {cpf_padded} processado. Encontradas {len(results)} matrículas.')

                except PlaywrightTimeoutError:
                    print(f'[AVISO] CPF {cpf_padded} não encontrado (Timeout).')
                    indices = df[df['CPF'] == cpf].index.tolist()
                    for idx in indices:
                        for col in colunas_extracao:
                            df.at[idx, col] = 'NÃO ENCONTRADO'
                        
                except Exception as e:
                    print(f'[ERRO] Falha ao processar CPF {cpf_padded}: {e}')
                    indices = df[df['CPF'] == cpf].index.tolist()
                    for idx in indices:
                        for col in colunas_extracao:
                            df.at[idx, col] = 'ERRO'

                # Salva o progresso a cada 5 registros ou no último
                if (idx_cpf + 1) % 5 == 0 or (idx_cpf + 1) == total_cpfs_to_process:
                    try:
                        df = df.sort_values(by=['CPF', 'Matricula']).reset_index(drop=True)
                        df.to_excel(temp_file, index=False)
                        print('[INFO] Progresso incremental salvo no temp_file.')
                    except Exception as e:
                        print(f'[ERRO] Falha ao salvar progresso incremental: {e}')

                # Telegram progress report a cada 500 CPFs
                if (idx_cpf + 1) % 500 == 0:
                    send_telegram_message(f"📈 *Status do Robô ({convenio.upper()}):*\n*Progresso:* {idx_cpf + 1}/{total_cpfs_to_process} CPFs consultados nesta rodada.")

            # Se completou o loop inteiro sem interromper
            else:
                print('[INFO] Execução de todo o lote concluída com sucesso!')
                df = df.sort_values(by=['CPF', 'Matricula']).reset_index(drop=True)
                df.to_excel(output_file, index=False)
                if temp_file.exists():
                    temp_file.unlink()
                send_telegram_message(f"✅ *Execução Concluída com Sucesso!* ({convenio.upper()})")
                send_telegram_document(output_file, f"📊 *Resultados Finais — {convenio.upper()}*")

        except Exception as e:
            print(f"[ERRO] Erro na execução geral: {e}")
            traceback.print_exc()
            send_telegram_message(f"🚨 *Erro Crítico no Robô {convenio.upper()}:* {e}")
        finally:
            context.close()

def main(config: dict, input_file: Path, temp_file: Path, output_file: Path) -> None:
    stop = threading.Event()
    _orig = signal.getsignal(signal.SIGINT)

    def _handle(*_):
        print("\n\nCtrl+C recebido — encerrando após o registro atual...")
        stop.set()

    signal.signal(signal.SIGINT, _handle)

    try:
        run(config, Path(input_file), Path(temp_file), Path(output_file), stop)
    finally:
        signal.signal(signal.SIGINT, _orig)

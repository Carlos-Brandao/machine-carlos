import os
import time
import signal
import sys
import datetime
import traceback
import threading
import subprocess
import unicodedata
from pathlib import Path
import pandas as pd
import requests
from playwright.sync_api import sync_playwright, Page, TimeoutError as PlaywrightTimeoutError

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

# --- LOGIN & EXTRAÇÃO CONSIGLOG ---

def login_consiglog(page: Page, login_url: str, usuario: str, senha: str) -> bool:
    """Executa o login no portal Consiglog com suporte a 2 etapas, modais e seleção de órgão."""
    print(f"[INFO] Acessando página de login Consiglog: {login_url}")
    page.goto(login_url, timeout=30000)
    page.wait_for_load_state("domcontentloaded")

    start_time = time.time()
    while time.time() - start_time < 45:
        url = page.url.lower()

        # 1. Se caiu na tela de Erro.aspx -> recarrega login limpo
        if "erro.aspx" in url:
            print("[LOGIN] Redirecionado para Erro.aspx. Voltando para a tela de login...")
            page.goto(login_url, timeout=30000)
            page.wait_for_load_state("domcontentloaded")
            continue

        # 2. Se já saiu de todas as telas de login e erro -> sucesso!
        if not any(p in url for p in ["login.aspx", "loginsegundaetapa", "loginselecao", "erro.aspx"]):
            print(f"[INFO] Login verificado com sucesso! URL atual: {page.url}")
            return True

        # 3. Modal "Usuário já logado" (prioridade alta)
        btn_confirmar_sessao = page.locator('input#ucAjaxModalPopupConfirmacao1_btnConfirmarPopup')
        if btn_confirmar_sessao.count() > 0 and btn_confirmar_sessao.first.is_visible():
            print("[LOGIN] Modal de 'Usuário já logado' detectada. Confirmando desconexão...")
            try:
                btn_confirmar_sessao.first.click()
            except Exception:
                page.evaluate("if(document.getElementById('ucAjaxModalPopupConfirmacao1_btnConfirmarPopup')) document.getElementById('ucAjaxModalPopupConfirmacao1_btnConfirmarPopup').click()")
            page.wait_for_timeout(2500)
            page.wait_for_load_state("domcontentloaded")
            continue

        # 4. Tela 1: Login.aspx (Usuário)
        if ("login.aspx" in url or "login" in url) and page.locator('input#txtLogin').is_visible() and not page.locator('input#txtLogin').is_disabled():
            print("[LOGIN] Preenchendo campo de usuário...")
            page.fill('input#txtLogin', usuario)
            page.click('input#Entrar')
            page.wait_for_timeout(2000)
            page.wait_for_load_state("domcontentloaded")
            continue

        # 5. Tela 2: LoginSegundaEtapa.aspx (Senha)
        if page.locator('input#txtSenha').is_visible():
            print("[LOGIN] Preenchendo campo de senha...")
            page.fill('input#txtSenha', senha)
            page.click('input#Entrar')
            page.wait_for_timeout(2500)
            page.wait_for_load_state("domcontentloaded")
            continue

        # 6. Tela 3: LoginSelecao.aspx (Órgão + Modal OK)
        if "loginselecao" in url or page.locator('input[id*="imgEntrar"]').count() > 0:
            btn_orgao = page.locator('input[id*="imgEntrar"]')
            if btn_orgao.count() > 0 and btn_orgao.first.is_visible():
                print("[LOGIN] Selecionando órgão (input#gvOrgao_imgEntrar_0)...")
                btn_orgao.first.click()
                page.wait_for_timeout(2000)

            btn_ok = page.locator('input#ucAjaxModalPopup1_btnConfirmarPopup')
            if btn_ok.count() > 0 and btn_ok.first.is_visible():
                print("[LOGIN] Clicando OK na modal de confirmação do órgão...")
                btn_ok.first.click()
                try:
                    page.wait_for_url(lambda u: "inicial" in u.lower() or "erro" in u.lower() or "margem" in u.lower(), timeout=10000)
                except Exception:
                    page.wait_for_timeout(3000)
                page.wait_for_load_state("domcontentloaded")
            continue

        page.wait_for_timeout(1000)

    print(f"[ERRO] Timeout no login Consiglog. Permanecemos na URL: {page.url}")
    return False

def remover_acentos(texto: str) -> str:
    nfkd = unicodedata.normalize('NFKD', texto)
    return "".join([c for c in nfkd if not unicodedata.combining(c)]).upper().strip()

def extrair_dados_margem(page: Page) -> dict:
    """Extrai os dados cadastrais e as margens com colunas padronizadas do Consiglog."""
    dados = {
        "Matricula": "",
        "Categoria": "",
        "Lotacao": "",
        "Situacao": "",
        "MARGEM EMPRESTIMO TOTAL": "",
        "MARGEM EMPRESTIMO RESERVADA": "",
        "MARGEM EMPRESTIMO DISPONIVEL": "",
        "MARGEM BENEFICIO COMPRA TOTAL": "",
        "MARGEM BENEFICIO COMPRA RESERVADA": "",
        "MARGEM BENEFICIO COMPRA DISPONIVEL": "",
        "MARGEM BENEFICIO SAQUE TOTAL": "",
        "MARGEM BENEFICIO SAQUE RESERVADA": "",
        "MARGEM BENEFICIO SAQUE DISPONIVEL": "",
        "MARGEM EVENTUAIS TOTAL": "",
        "MARGEM EVENTUAIS RESERVADA": "",
        "MARGEM EVENTUAIS DISPONIVEL": "",
        "MARGEM BENEFICIO TOTAL": "",
        "MARGEM BENEFICIO RESERVADA": "",
        "MARGEM BENEFICIO DISPONIVEL": "",
    }

    def _get_val(selector: str) -> str:
        try:
            loc = page.locator(selector)
            if loc.count() > 0:
                val = loc.first.input_value() if loc.first.evaluate("el => el.tagName") == "INPUT" else loc.first.inner_text()
                return val.strip()
        except Exception:
            pass
        return ""

    dados["Matricula"] = _get_val('[id*="matriculaTextBox"]') or _get_val("input[name*='matriculaTextBox']")
    dados["Categoria"] = _get_val('[id*="categoriaTextBox"]') or _get_val("input[name*='categoriaTextBox']")
    dados["Lotacao"] = _get_val('[id*="txtLotacao"]') or _get_val("input[name*='txtLotacao']")
    dados["Situacao"] = _get_val('[id*="txtSituacao"]') or _get_val("input[name*='txtSituacao']")

    # Raspa as linhas da tabela de margens (índices 0 a 9)
    for i in range(10):
        hdr_sel = f'[id*="headerservico_{i}"], [id*="headerservico"][id$="_{i}"]'
        hdr_loc = page.locator(hdr_sel)
        if hdr_loc.count() > 0:
            texto_bruto = hdr_loc.first.inner_text()
            if texto_bruto:
                texto_limpo = texto_bruto.replace("\n", "\t")
                partes = [p.strip() for p in texto_limpo.split("\t") if p.strip()]

                if len(partes) >= 4:
                    nome_servico = remover_acentos(partes[0])
                    val_total = partes[1]
                    val_reservada = partes[2]
                    val_disponivel = partes[3]

                    if "EMPRESTIMO" in nome_servico:
                        dados["MARGEM EMPRESTIMO TOTAL"] = val_total
                        dados["MARGEM EMPRESTIMO RESERVADA"] = val_reservada
                        dados["MARGEM EMPRESTIMO DISPONIVEL"] = val_disponivel
                    elif "BENEFICIO COMPRA" in nome_servico:
                        dados["MARGEM BENEFICIO COMPRA TOTAL"] = val_total
                        dados["MARGEM BENEFICIO COMPRA RESERVADA"] = val_reservada
                        dados["MARGEM BENEFICIO COMPRA DISPONIVEL"] = val_disponivel
                    elif "BENEFICIO SAQUE" in nome_servico:
                        dados["MARGEM BENEFICIO SAQUE TOTAL"] = val_total
                        dados["MARGEM BENEFICIO SAQUE RESERVADA"] = val_reservada
                        dados["MARGEM BENEFICIO SAQUE DISPONIVEL"] = val_disponivel
                    elif "EVENTUAIS" in nome_servico:
                        dados["MARGEM EVENTUAIS TOTAL"] = val_total
                        dados["MARGEM EVENTUAIS RESERVADA"] = val_reservada
                        dados["MARGEM EVENTUAIS DISPONIVEL"] = val_disponivel
                    elif "BENEFICIO" in nome_servico:
                        dados["MARGEM BENEFICIO TOTAL"] = val_total
                        dados["MARGEM BENEFICIO RESERVADA"] = val_reservada
                        dados["MARGEM BENEFICIO DISPONIVEL"] = val_disponivel

    return dados

# --- MAIN BOT RUNNER ---

def run(config: dict, input_file: Path, temp_file: Path, output_file: Path, stop: threading.Event) -> None:
    login_url = config.get("url_login", os.getenv("CONSIGLOG_URL_LOGIN", "https://saec.consigx.com.br/Login.aspx"))
    consulta_url = config.get("url_consulta", os.getenv("CONSIGLOG_URL_CONSULTA", "https://saec.consigx.com.br/Margem/ConsultaMargem.aspx"))
    usuario = config["usuario"]
    senha = config["senha"]
    convenio = config.get("convenio", "itabuna").lower()

    if temp_file.exists():
        print(f"[INFO] Carregando progresso anterior de {temp_file}")
        df = pd.read_excel(temp_file, dtype=str)
    else:
        print(f"[INFO] Iniciando novo processamento. Copiando de {input_file}")
        df = pd.read_excel(input_file, dtype=str)

        colunas_base = [
            'Matricula', 'Categoria', 'Lotacao', 'Situacao',
            'MARGEM EMPRESTIMO TOTAL', 'MARGEM EMPRESTIMO RESERVADA', 'MARGEM EMPRESTIMO DISPONIVEL',
            'MARGEM BENEFICIO COMPRA TOTAL', 'MARGEM BENEFICIO COMPRA RESERVADA', 'MARGEM BENEFICIO COMPRA DISPONIVEL',
            'MARGEM BENEFICIO SAQUE TOTAL', 'MARGEM BENEFICIO SAQUE RESERVADA', 'MARGEM BENEFICIO SAQUE DISPONIVEL',
            'MARGEM EVENTUAIS TOTAL', 'MARGEM EVENTUAIS RESERVADA', 'MARGEM EVENTUAIS DISPONIVEL',
            'MARGEM BENEFICIO TOTAL', 'MARGEM BENEFICIO RESERVADA', 'MARGEM BENEFICIO DISPONIVEL',
            'Status_Robo'
        ]
        for col in colunas_base:
            if col not in df.columns:
                df[col] = None
        df.to_excel(temp_file, index=False)

    coluna_cpf = next((c for c in df.columns if c.upper() == "CPF"), None)
    if not coluna_cpf:
        print("[ERRO] A planilha de entrada precisa ter a coluna 'CPF'.")
        return

    # Identifica CPFs já processados
    processed_cpfs = set()
    for _, row in df.iterrows():
        status = str(row.get('Status_Robo', '')).strip()
        if status in ['Sucesso', 'NÃO ENCONTRADO']:
            processed_cpfs.add(row[coluna_cpf])

    unique_cpfs = df[coluna_cpf].dropna().unique()
    unprocessed_cpfs = [cpf for cpf in unique_cpfs if cpf not in processed_cpfs]
    total_cpfs_to_process = len(unprocessed_cpfs)

    print(f'[INFO] Bot CONSIGLOG — Convênio: {convenio.upper()}')
    print(f'[INFO] CPFs já processados: {len(processed_cpfs)}')
    print(f'[INFO] CPFs a processar: {total_cpfs_to_process}')
    send_telegram_message(
        f"🚀 *Robô Consiglog ({convenio.upper()}) Iniciado!*\n"
        f"- Total do Lote: {len(unique_cpfs)} CPFs\n"
        f"- Já processados: {len(processed_cpfs)}\n"
        f"- A processar: {total_cpfs_to_process}"
    )

    if total_cpfs_to_process == 0:
        print("[INFO] Todos os registros já foram processados.")
        df.to_excel(output_file, index=False)
        if temp_file.exists():
            temp_file.unlink()
        return

    profile_dir = Path(__file__).parent.parent / "chrome_profile"
    headless_mode = os.environ.get('HEADLESS', 'False').lower() == 'true'

    with sync_playwright() as p:
        try:
            print(f'[INFO] Inicializando navegador Chrome (Headless={headless_mode})...')
            try:
                context = p.chromium.launch_persistent_context(
                    user_data_dir=str(profile_dir),
                    headless=headless_mode,
                    channel="chrome",
                    viewport={"width": 1280, "height": 720},
                    args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-gpu"]
                )
            except Exception:
                context = p.chromium.launch_persistent_context(
                    user_data_dir=str(profile_dir),
                    headless=headless_mode,
                    viewport={"width": 1280, "height": 720},
                    args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-gpu"]
                )
            
            page = context.pages[0]
            page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
            
        except Exception as e:
            print(f'[ERRO] Falha ao inicializar o navegador: {e}')
            send_telegram_message(f"❌ *Erro de Inicialização:* {e}")
            return

        try:
            if not login_consiglog(page, login_url, usuario, senha):
                send_telegram_message(f"❌ *Falha no Login:* Não foi possível conectar ao Consiglog ({convenio.upper()}).")
                return

            send_telegram_message(f"✅ *Login efetuado com sucesso no Consiglog ({convenio.upper()})!*")

            for idx_cpf, cpf in enumerate(unprocessed_cpfs):
                if stop.is_set():
                    print("[INFO] Interrupção (Ctrl+C) detectada.")
                    send_telegram_message(f"⏳ *Execução Pausada:* O robô Consiglog ({convenio.upper()}) foi interrompido.")
                    break

                raw_cpf = str(cpf).strip().split('.')[0].split('-')[0]
                cpf_padded = raw_cpf.zfill(11)

                print(f'[{idx_cpf + 1}/{total_cpfs_to_process}] Consultando CPF: {cpf_padded}')

                try:
                    if "ConsultaMargem" not in page.url:
                        print(f"[INFO] Navegando para tela de consulta a partir de {page.url}...")
                        
                        # 1. Fecha modais de aviso/pendência se existirem na Inicial.aspx
                        page.evaluate("""() => {
                            const btns = Array.from(document.querySelectorAll('input[id*="btnConfirmarPopup"], input[id*="btnOK"]'));
                            btns.forEach(b => { if (b.offsetWidth > 0 && b.offsetHeight > 0) b.click(); });
                        }""")
                        page.wait_for_timeout(1500)

                        # 2. Expande o menu 'Margem' e clica no subitem de Consulta
                        page.evaluate("""() => {
                            // Clica no item pai 'Margem' para expandir
                            const parent = Array.from(document.querySelectorAll('a')).find(a => a.innerText.includes('Margem') || a.href.includes('#Margem'));
                            if (parent) parent.click();
                        }""")
                        page.wait_for_timeout(1000)

                        # 3. Clica no link real para ConsultaMargemDados.aspx
                        page.evaluate("""() => {
                            const sub = Array.from(document.querySelectorAll('a')).find(a => a.href.includes('ConsultaMargem') && !a.href.endsWith('.pdf'));
                            if (sub) sub.click();
                        }""")
                        page.wait_for_timeout(3000)
                        page.wait_for_load_state("domcontentloaded")

                        # Se ainda não for para ConsultaMargem, tenta o goto direto agora que a sessão foi inicializada no menu
                        if "ConsultaMargem" not in page.url:
                            print(f"[INFO] Navegando diretamente para a URL de consulta: {consulta_url}")
                            page.goto(consulta_url, timeout=20000)
                            page.wait_for_load_state("domcontentloaded")

                    # 1. Garante que estamos na tela com o campo de CPF limpo e pronto
                    cpf_loc = page.locator('input#body_cpfTextBox, input[name*="cpfTextBox"]')
                    if cpf_loc.count() == 0 or not cpf_loc.first.is_visible():
                        btn_canc = page.locator('input[id*="cancelarButton"], input[id*="btnVoltar"], input[value*="Voltar"], input[value*="Cancelar"]')
                        if btn_canc.count() > 0 and btn_canc.first.is_visible():
                            try:
                                btn_canc.first.click()
                                page.wait_for_timeout(1500)
                                page.wait_for_load_state("domcontentloaded")
                            except Exception:
                                pass
                        
                    # Se caiu na tela de login por expiração de sessão, faz login novamente
                    if "Login" in page.url or page.locator('input#txtLogin').count() > 0:
                        print("[INFO] Sessão expirada detectada. Efetuando re-login no portal...")
                        login_consiglog(page, login_url, usuario, senha)
                        page.goto(consulta_url, timeout=20000)
                        page.wait_for_load_state("domcontentloaded")

                    cpf_selector = 'input#body_cpfTextBox, input[name*="cpfTextBox"]'
                    btn_selector = 'input#body_pesquisarButton, input[id*="pesquisarButton"], input[id*="btnConsultar"], input[name*="btnConsultar"]'
                    page.wait_for_selector(cpf_selector, state='visible', timeout=15000)
                    page.fill(cpf_selector, "")
                    page.fill(cpf_selector, cpf_padded)
                    page.click(btn_selector)

                    page.wait_for_timeout(3000)
                    try:
                        page.wait_for_load_state("networkidle", timeout=10000)
                    except Exception:
                        pass

                    # Checa se encontrou os dados ou se exibiu erro
                    if page.locator('[id*="matriculaTextBox"]').count() > 0 or page.locator('[id*="rptMargens"]').count() > 0:
                        # Verifica especificamente se há múltiplos elementos/opções de seleção de matrícula na página
                        multiplas_matrculas = []
                        selects = page.locator('select[id*="matricula"], select[id*="Matricula"], select[id*="ddl"], select[name*="matricula"]')
                        for s_idx in range(selects.count()):
                            sel_el = selects.nth(s_idx)
                            opt_texts = [o.strip() for o in sel_el.locator('option').all_inner_texts() if o.strip()]
                            if len(opt_texts) > 1:
                                multiplas_matrculas.append(f"Select #{s_idx} ({sel_el.evaluate('el => el.id')}): {opt_texts}")

                        grid_tables = page.locator('table[id*="gvMatricula"], table[id*="gvServidor"], table[id*="gvSelecao"], table[id*="gvVinculo"]')
                        for g_idx in range(grid_tables.count()):
                            t_el = grid_tables.nth(g_idx)
                            rows = [r.inner_text().strip() for r in t_el.locator('tr').all() if r.inner_text().strip()]
                            if len(rows) > 2:
                                multiplas_matrculas.append(f"Grid #{g_idx} ({t_el.evaluate('el => el.id')}): {rows}")

                        dados_extraidos = extrair_dados_margem(page)
                        status_final = f"Sucesso (Múltiplas Matrículas: {len(multiplas_matrculas)})" if multiplas_matrculas else "Sucesso"
                        if multiplas_matrculas:
                            print(f"[DETECÇÃO MÚLTIPLA] CPF {cpf} possui MÚLTIPLAS MATRÍCULAS/OPÇÕES: {multiplas_matrculas}")

                        matching_rows = df[df[coluna_cpf] == cpf]
                        for idx in matching_rows.index:
                            for k, v in dados_extraidos.items():
                                if k in df.columns:
                                    df.at[idx, k] = v
                            df.at[idx, "Status_Robo"] = status_final

                        print(f"[SUCESSO] CPF {cpf_padded} consultado. Matrícula: {dados_extraidos.get('Matricula', 'N/A')}")

                    else:
                        print(f'[AVISO] CPF {cpf_padded} não localizado ou sem dados.')
                        matching_rows = df[df[coluna_cpf] == cpf]
                        for idx in matching_rows.index:
                            df.at[idx, "Status_Robo"] = "NÃO ENCONTRADO"

                except PlaywrightTimeoutError as err:
                    print(f'[AVISO] Timeout ao consultar CPF {cpf_padded}: {err}')
                    matching_rows = df[df[coluna_cpf] == cpf]
                    for idx in matching_rows.index:
                        df.at[idx, 'Status_Robo'] = 'TIMEOUT'

                except Exception as e:
                    print(f'[ERRO] Falha no CPF {cpf_padded}: {e}')
                    matching_rows = df[df[coluna_cpf] == cpf]
                    for idx in matching_rows.index:
                        df.at[idx, 'Status_Robo'] = 'ERRO'

                # Salva o progresso incremental a cada 5 CPFs ou no último
                if (idx_cpf + 1) % 5 == 0 or (idx_cpf + 1) == total_cpfs_to_process:
                    try:
                        df.to_excel(temp_file, index=False)
                        print('[INFO] Progresso incremental salvo no temp_file.')
                    except Exception as e:
                        print(f'[ERRO] Falha ao salvar progresso incremental: {e}')

                # Envia planilha parcial no Telegram a cada 500 CPFs
                if (idx_cpf + 1) % 500 == 0:
                    try:
                        qtd_proc = len(df[df['Status_Robo'].notna()])
                        qtd_sucesso = len(df[df['Status_Robo'] == 'Sucesso'])
                        caption = (
                            f"📊 *Progresso Parcial Consiglog ({convenio.upper()})*\n"
                            f"- Consultados: {qtd_proc} / {len(df)} ({(qtd_proc/len(df))*100:.1f}%)\n"
                            f"- Com Sucesso: {qtd_sucesso}"
                        )
                        send_telegram_document(temp_file, caption=caption)
                        print('[INFO] Planilha parcial enviada no Telegram.')
                    except Exception as e:
                        print(f'[ERRO] Falha ao enviar parcial no Telegram: {e}')

            else:
                print('[INFO] Execução de todo o lote concluída com sucesso!')
                df.to_excel(output_file, index=False)
                if temp_file.exists():
                    temp_file.unlink()
                send_telegram_message(f"✅ *Execução Consiglog ({convenio.upper()}) Concluída!*")
                send_telegram_document(output_file, f"📊 *Resultados Consiglog — {convenio.upper()}*")

        except Exception as e:
            print(f"[ERRO] Erro na execução geral: {e}")
            traceback.print_exc()
            send_telegram_message(f"🚨 *Erro Crítico no Robô Consiglog:* {e}")
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

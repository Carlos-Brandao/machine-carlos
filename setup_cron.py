import os
import sys

# Credenciais herdadas da pasta Fortaleza
IP = "187.127.4.57"
USER = "root"
PASSWORD = "Kbzci;(0XK)TRTbA"
REMOTE_DIR = "/root/ROBO_FACIL"

def setup_vps():
    try:
        import paramiko
    except ImportError:
        print("[ERRO] Biblioteca 'paramiko' não instalada no ambiente local.")
        print("Instale usando: pip install paramiko")
        return False

    print(f"Conectando ao VPS {IP}...")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(IP, username=USER, password=PASSWORD, timeout=10)
    except Exception as e:
        print(f"[ERRO] Falha ao conectar ao VPS: {e}")
        return False

    # Comando Cron para rodar somente em dias úteis, às 07:00 BRT.
    # A flag --cron gerencia automaticamente o intervalo de 15 dias e retomadas automáticas
    cron_line = f"0 7 * * 1-5 cd {REMOTE_DIR} && xvfb-run -a --server-args='-screen 0 1280x720x24' ./env/bin/python main.py facil paulista --cron >> cron_facil.log 2>&1"

    print("Buscando crontab atual no VPS...")
    stdin, stdout, stderr = ssh.exec_command("crontab -l")
    current_cron = stdout.read().decode().strip()
    
    cron_lines = []
    if current_cron and "no crontab for" not in current_cron:
        # Mantém as linhas atuais, excluindo qualquer comando antigo do ROBO_FACIL para evitar duplicatas
        cron_lines = [line for line in current_cron.split("\n") if REMOTE_DIR not in line]
        
    cron_lines.append(cron_line)
    new_cron = "\n".join(cron_lines) + "\n"

    print("Escrevendo novas configurações do crontab no VPS...")
    sftp = ssh.open_sftp()
    try:
        with sftp.file("/tmp/cron_temp", "w") as f:
            f.write(new_cron)
    except Exception as e:
        print(f"[ERRO] Falha ao transferir arquivo temporário do cron: {e}")
        ssh.close()
        return False
    sftp.close()

    stdin, stdout, stderr = ssh.exec_command("crontab /tmp/cron_temp && rm /tmp/cron_temp")
    
    # Validar se atualizou
    stdin, stdout, stderr = ssh.exec_command("crontab -l")
    print("\n--- NOVO CRONTAB DA VPS ---")
    print(stdout.read().decode())
    
    ssh.close()
    print("Configuração concluída com sucesso no VPS!")
    return True

def setup_local():
    print("Configurando crontab local (macOS)...")
    import subprocess
    
    cron_line = f'0 7 * * * cd "{os.getcwd()}" && ./env/bin/python main.py facil paulista --cron >> cron_facil.log 2>&1'
    
    try:
        current_cron = subprocess.check_output("crontab -l", shell=True, stderr=subprocess.DEVNULL).decode().strip()
    except subprocess.CalledProcessError:
        current_cron = ""

    cron_lines = []
    if current_cron:
        cron_lines = [line for line in current_cron.split("\n") if os.getcwd() not in line]
        
    cron_lines.append(cron_line)
    new_cron = "\n".join(cron_lines) + "\n"

    temp_file = "/tmp/cron_local_temp"
    with open(temp_file, "w") as f:
        f.write(new_cron)

    try:
        subprocess.check_call(f"crontab {temp_file} && rm {temp_file}", shell=True)
        print("\n--- NOVO CRONTAB LOCAL ---")
        print(subprocess.check_output("crontab -l", shell=True).decode())
        print("Configuração local concluída com sucesso!")
    except Exception as e:
        print(f"[ERRO] Falha ao instalar crontab local: {e}")

if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == "--vps":
        setup_vps()
    elif len(sys.argv) > 1 and sys.argv[1] == "--local":
        setup_local()
    else:
        print("Uso do script:")
        print("  python setup_cron.py --local   <- Configura crontab na sua máquina macOS atual")
        print("  python setup_cron.py --vps     <- Configura crontab na VPS remota (via SSH)")

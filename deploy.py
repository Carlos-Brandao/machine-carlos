import os
import sys
import paramiko
from scp import SCPClient

IP = "187.127.4.57"
USER = "root"
PASSWORD = "Kbzci;(0XK)TRTbA"
REMOTE_DIR = "/root/ROBO_FACIL"

def create_ssh_client():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(IP, username=USER, password=PASSWORD, timeout=15)
    return ssh

def main():
    print(f"[1/5] Conectando a {USER}@{IP}...")
    try:
        ssh = create_ssh_client()
    except Exception as e:
        print(f"Erro na conexão SSH: {e}")
        return

    # Criando diretório na VPS
    ssh.exec_command(f"mkdir -p {REMOTE_DIR}/data {REMOTE_DIR}/temp {REMOTE_DIR}/completed")

    print("[2/5] Transferindo arquivos para a VPS...")
    with SCPClient(ssh.get_transport()) as scp:
        # Envia arquivos na raiz
        for item in ['.env', 'main.py', 'requirements.txt', 'setup_cron.py']:
            if os.path.exists(item):
                print(f"  -> Transferindo {item}")
                scp.put(item, remote_path=REMOTE_DIR)
        
        # Envia pastas recursivamente
        for folder in ['facil', 'services', 'data']:
            if os.path.exists(folder):
                print(f"  -> Transferindo {folder}/")
                scp.put(folder, recursive=True, remote_path=REMOTE_DIR)

    print("[3/5] Configurando ambiente virtual Python na VPS...")
    # Executa a criação e instalação das dependências
    setup_commands = [
        f"cd {REMOTE_DIR} && [ ! -d env ] && python3 -m venv env || echo 'Virtualenv já existe'",
        f"cd {REMOTE_DIR} && ./env/bin/pip install -r requirements.txt",
        f"cd {REMOTE_DIR} && ./env/bin/playwright install --with-deps chromium"
    ]
    
    for cmd in setup_commands:
        print(f"  Executando: {cmd}")
        stdin, stdout, stderr = ssh.exec_command(cmd)
        stdout.channel.recv_exit_status() # Aguarda a conclusão

    print("[4/5] Configurando crontab na VPS...")
    # Roda o script setup_cron.py na VPS para atualizar o agendamento
    stdin, stdout, stderr = ssh.exec_command(f"cd {REMOTE_DIR} && ./env/bin/python setup_cron.py --local")
    stdout.channel.recv_exit_status()

    print("[5/5] Iniciando o Robô em Background na VPS...")
    # Mata qualquer processo anterior do main.py na VPS antes de rodar
    ssh.exec_command("pkill -f 'main.py' || true")
    
    # Roda o script em background usando xvfb-run
    run_cmd = f"cd {REMOTE_DIR} && nohup xvfb-run -a --server-args='-screen 0 1280x720x24' ./env/bin/python -u main.py facil paulista --file data/base_paulista_prev.xlsx -y > nohup.out 2>&1 &"
    ssh.exec_command(run_cmd)

    print("\n✅ Deploy Finalizado com Sucesso!")
    print(f"O robô está rodando em background na VPS em {REMOTE_DIR}.")
    print("Acompanhe os logs remotamente executando na VPS:")
    print(f"  tail -f {REMOTE_DIR}/nohup.out")
    
    ssh.close()

if __name__ == '__main__':
    main()

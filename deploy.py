"""Sincroniza a aplicação com a VPS sem iniciar ou matar processos."""

from __future__ import annotations

import shlex
from pathlib import Path

from dotenv import load_dotenv
from scp import SCPClient

from services.remote import (
    RemoteConfigurationError,
    RemoteSettings,
    create_ssh_client,
)


ROOT = Path(__file__).parent
ROOT_FILES = (
    "alembic.ini",
    "main.py",
    "requirements.txt",
    "run_admin.py",
    "run_backend_api.py",
    "run_scheduler.py",
    "run_telegram_bot.py",
    "run_worker.py",
)
SOURCE_DIRS = (
    "deploy",
    "consiglog",
    "facil",
    "grid",
    "machine_admin",
    "migrations",
    "rf1",
    "safeconsig",
    "services",
    "workers",
)


def _run(ssh, command: str) -> None:
    _, stdout, stderr = ssh.exec_command(command)
    exit_code = stdout.channel.recv_exit_status()
    if exit_code:
        message = stderr.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError(message or f"Comando remoto falhou com código {exit_code}.")


def main() -> None:
    load_dotenv(ROOT / ".env")
    try:
        settings = RemoteSettings.from_environment()
        ssh = create_ssh_client(settings)
    except (RemoteConfigurationError, OSError) as exc:
        raise SystemExit(f"Falha ao conectar à VPS: {exc}") from exc

    remote_dir = settings.remote_dir
    quoted_dir = shlex.quote(remote_dir)
    try:
        _run(
            ssh,
            f"mkdir -p {quoted_dir}/storage {quoted_dir}/job_logs "
            f"{quoted_dir}/data {quoted_dir}/temp {quoted_dir}/completed",
        )
        with SCPClient(ssh.get_transport()) as scp:
            for filename in ROOT_FILES:
                source = ROOT / filename
                if source.exists():
                    print(f"Enviando {filename}")
                    scp.put(str(source), remote_path=remote_dir)
            for directory in SOURCE_DIRS:
                source = ROOT / directory
                if source.exists():
                    print(f"Enviando {directory}/")
                    scp.put(str(source), recursive=True, remote_path=remote_dir)

        _run(
            ssh,
            f"cd {quoted_dir} && "
            "([ -d env ] || python3 -m venv env) && "
            "./env/bin/pip install -r requirements.txt && "
            "./env/bin/playwright install chromium",
        )
    finally:
        ssh.close()

    print("Arquivos e dependências sincronizados.")
    print("Próximos passos manuais na VPS, após conferir o banco:")
    print(f"  cd {remote_dir} && ./env/bin/alembic upgrade head")
    print("  sudo systemctl daemon-reload")
    print("  sudo systemctl restart machine-backend machine-scheduler")


if __name__ == "__main__":
    main()

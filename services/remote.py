"""Configuração segura para tarefas administrativas remotas."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class RemoteConfigurationError(RuntimeError):
    """As credenciais de administração remota não foram configuradas."""


@dataclass(frozen=True)
class RemoteSettings:
    host: str
    username: str
    remote_dir: str
    password: str | None
    key_filename: str | None

    @classmethod
    def from_environment(cls) -> "RemoteSettings":
        host = os.getenv("MACHINE_SSH_HOST", "").strip()
        username = os.getenv("MACHINE_SSH_USER", "").strip()
        password = os.getenv("MACHINE_SSH_PASSWORD", "") or None
        key_filename = os.getenv("MACHINE_SSH_KEY_FILE", "").strip() or None
        if not host or not username:
            raise RemoteConfigurationError(
                "Defina MACHINE_SSH_HOST e MACHINE_SSH_USER no ambiente."
            )
        if not password and not key_filename:
            raise RemoteConfigurationError(
                "Defina MACHINE_SSH_KEY_FILE (preferível) ou MACHINE_SSH_PASSWORD."
            )
        if key_filename and not Path(key_filename).expanduser().is_file():
            raise RemoteConfigurationError("MACHINE_SSH_KEY_FILE não aponta para um arquivo válido.")
        return cls(
            host=host,
            username=username,
            remote_dir=os.getenv("MACHINE_REMOTE_DIR", "/root/ROBO_FACIL").strip(),
            password=password,
            key_filename=str(Path(key_filename).expanduser()) if key_filename else None,
        )


def create_ssh_client(settings: RemoteSettings):
    try:
        import paramiko
    except ImportError as exc:
        raise RemoteConfigurationError("paramiko não está instalado.") from exc

    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    client.connect(
        settings.host,
        username=settings.username,
        password=settings.password,
        key_filename=settings.key_filename,
        timeout=15,
    )
    return client

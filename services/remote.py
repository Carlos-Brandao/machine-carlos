"""Configuração segura para tarefas administrativas remotas.

``MACHINE_REMOTE_DIR`` continua representando a instalação legada. Novos
deployments usam ``/opt/machine`` e nunca gravam código sobre a versão ativa.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


class RemoteConfigurationError(RuntimeError):
    """As credenciais de administração remota não foram configuradas."""


@dataclass(frozen=True)
class RemoteSettings:
    host: str
    username: str
    remote_dir: str
    password: str | None
    key_filename: str | None
    port: int = 22
    release_root: str = "/opt/machine"
    service_user: str = "machine"
    service_group: str = "machine"
    keep_releases: int = 5
    health_url: str = "http://127.0.0.1:8000/health"

    @property
    def legacy_remote_dir(self) -> str:
        """Nome explícito para o caminho usado antes dos releases."""

        return self.remote_dir

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
            raise RemoteConfigurationError(
                "MACHINE_SSH_KEY_FILE não aponta para um arquivo válido."
            )
        try:
            port = int(os.getenv("MACHINE_SSH_PORT", "22"))
            keep_releases = int(os.getenv("MACHINE_KEEP_RELEASES", "5"))
        except ValueError as exc:
            raise RemoteConfigurationError(
                "MACHINE_SSH_PORT e MACHINE_KEEP_RELEASES devem ser inteiros."
            ) from exc
        if not 1 <= port <= 65535:
            raise RemoteConfigurationError("MACHINE_SSH_PORT deve estar entre 1 e 65535.")
        if not 2 <= keep_releases <= 20:
            raise RemoteConfigurationError("MACHINE_KEEP_RELEASES deve estar entre 2 e 20.")

        remote_dir = os.getenv("MACHINE_REMOTE_DIR", "/root/ROBO_FACIL").strip()
        release_root = os.getenv("MACHINE_RELEASE_ROOT", "/opt/machine").strip()
        for name, value in (
            ("MACHINE_REMOTE_DIR", remote_dir),
            ("MACHINE_RELEASE_ROOT", release_root),
        ):
            path = Path(value)
            if (
                not value
                or not path.is_absolute()
                or path == Path("/")
                or not re.fullmatch(r"/[A-Za-z0-9_./-]+", value)
            ):
                raise RemoteConfigurationError(
                    f"{name} deve ser um caminho absoluto simples e específico."
                )
            if ".." in path.parts:
                raise RemoteConfigurationError(f"{name} não pode conter '..'.")

        legacy_path = Path(remote_dir)
        release_path = Path(release_root)
        if (
            legacy_path == release_path
            or legacy_path in release_path.parents
            or release_path in legacy_path.parents
        ):
            raise RemoteConfigurationError(
                "MACHINE_REMOTE_DIR e MACHINE_RELEASE_ROOT não podem se sobrepor."
            )

        service_user = os.getenv("MACHINE_SERVICE_USER", "machine").strip()
        service_group = os.getenv("MACHINE_SERVICE_GROUP", service_user).strip()
        account_pattern = re.compile(r"^[a-z_][a-z0-9_-]{0,30}$")
        if not account_pattern.fullmatch(service_user) or not account_pattern.fullmatch(
            service_group
        ):
            raise RemoteConfigurationError(
                "MACHINE_SERVICE_USER/GROUP contém um nome de conta inválido."
            )

        health_url = os.getenv(
            "MACHINE_HEALTH_URL", "http://127.0.0.1:8000/health"
        ).strip()
        parsed_health = urlsplit(health_url)
        if (
            parsed_health.scheme != "http"
            or parsed_health.hostname not in {"127.0.0.1", "localhost", "::1"}
        ):
            raise RemoteConfigurationError(
                "MACHINE_HEALTH_URL deve ser uma URL HTTP local da VPS."
            )
        return cls(
            host=host,
            username=username,
            remote_dir=remote_dir,
            password=password,
            key_filename=str(Path(key_filename).expanduser()) if key_filename else None,
            port=port,
            release_root=release_root,
            service_user=service_user,
            service_group=service_group,
            keep_releases=keep_releases,
            health_url=health_url,
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
        port=settings.port,
        username=settings.username,
        password=settings.password,
        key_filename=settings.key_filename,
        timeout=15,
        banner_timeout=15,
        auth_timeout=15,
    )
    transport = client.get_transport()
    if transport is not None:
        transport.set_keepalive(30)
    return client

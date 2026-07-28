"""Configuração validada do painel e dos workers."""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from pathlib import Path

from services.database import require_postgres_url


class SettingsError(RuntimeError):
    """Configuração obrigatória ausente ou inválida."""


@dataclass(frozen=True)
class Settings:
    database_url: str
    session_secret: str
    master_key: bytes
    cookie_secure: bool
    allowed_hosts: tuple[str, ...]
    storage_dir: Path
    max_upload_bytes: int
    bootstrap_admin_email: str | None
    bootstrap_admin_password: str | None

    @classmethod
    def from_environment(cls) -> "Settings":
        session_secret = os.getenv("ADMIN_SESSION_SECRET", "").strip()
        raw_master_key = os.getenv("APP_MASTER_KEY", "").strip()
        if len(session_secret) < 32:
            raise SettingsError("ADMIN_SESSION_SECRET deve ter pelo menos 32 caracteres.")
        try:
            master_key = base64.urlsafe_b64decode(raw_master_key.encode("ascii"))
        except Exception as exc:
            raise SettingsError("APP_MASTER_KEY não é uma chave base64 URL-safe válida.") from exc
        if len(master_key) != 32:
            raise SettingsError("APP_MASTER_KEY deve decodificar exatamente 32 bytes.")

        email = os.getenv("BOOTSTRAP_ADMIN_EMAIL", "").strip().lower() or None
        password = os.getenv("BOOTSTRAP_ADMIN_PASSWORD", "") or None
        if bool(email) != bool(password):
            raise SettingsError(
                "BOOTSTRAP_ADMIN_EMAIL e BOOTSTRAP_ADMIN_PASSWORD devem ser definidos juntos."
            )
        if password and len(password) < 12:
            raise SettingsError("BOOTSTRAP_ADMIN_PASSWORD deve ter pelo menos 12 caracteres.")

        allowed_hosts = tuple(
            value.strip()
            for value in os.getenv("ADMIN_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
            if value.strip()
        )
        try:
            max_upload_bytes = int(
                os.getenv("MAX_UPLOAD_BYTES", str(25 * 1024 * 1024))
            )
        except ValueError as exc:
            raise SettingsError("MAX_UPLOAD_BYTES deve ser um número inteiro.") from exc
        if max_upload_bytes <= 0:
            raise SettingsError("MAX_UPLOAD_BYTES deve ser maior que zero.")
        if not allowed_hosts:
            raise SettingsError("ADMIN_ALLOWED_HOSTS precisa conter ao menos um host.")

        return cls(
            database_url=require_postgres_url(),
            session_secret=session_secret,
            master_key=master_key,
            cookie_secure=os.getenv("ADMIN_COOKIE_SECURE", "true").lower() == "true",
            allowed_hosts=allowed_hosts,
            storage_dir=Path(
                os.getenv("MACHINE_STORAGE_DIR", "storage")
            ).expanduser().resolve(),
            max_upload_bytes=max_upload_bytes,
            bootstrap_admin_email=email,
            bootstrap_admin_password=password,
        )

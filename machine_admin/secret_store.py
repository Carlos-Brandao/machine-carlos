"""Leitura de segredos operacionais com PostgreSQL como fonte preferencial."""

from __future__ import annotations

import os
from functools import lru_cache

from machine_admin.config import Settings
from machine_admin.db import get_session_factory
from machine_admin.models import IntegrationSecret
from machine_admin.services import decrypt_integration_secret


@lru_cache(maxsize=32)
def get_runtime_secret(key: str) -> str:
    """Busca um segredo cifrado e usa o ambiente durante a transição."""
    normalized_key = key.strip().upper()
    try:
        settings = Settings.from_environment()
        with get_session_factory()() as session:
            stored = session.get(IntegrationSecret, normalized_key)
            if stored:
                return decrypt_integration_secret(stored, settings)
    except Exception:
        # Bootstrap e indisponibilidade do banco continuam aceitando o .env.
        # O erro não é registrado para nunca vazar material sensível.
        pass
    return os.getenv(normalized_key, "").strip()


def clear_secret_cache() -> None:
    """Recarrega valores após uma rotação em processos longos."""
    get_runtime_secret.cache_clear()

"""Leitura de segredos operacionais com PostgreSQL como fonte preferencial."""

from __future__ import annotations

import os
import logging
from collections.abc import Callable

from sqlalchemy.exc import SQLAlchemyError

from machine_admin.config import Settings
from machine_admin.db import get_session_factory
from machine_admin.models import IntegrationSecret
from machine_admin.services import decrypt_integration_secret


LOG = logging.getLogger(__name__)
_remote_secret_provider: Callable[[str], str] | None = None


def configure_remote_secret_provider(
    provider: Callable[[str], str] | None,
) -> None:
    """Configura uma fonte remota autenticada para processos sem acesso ao DB.

    Workers e o controlador Telegram rodam com privilégio mínimo: eles não
    recebem ``DATABASE_URL`` nem ``APP_MASTER_KEY``. Nesses processos, o
    backend entrega apenas as chaves permitidas pelo escopo do token da API.
    Uma vez configurada, a fonte remota é autoritativa e falha fechada; usar
    silenciosamente um valor antigo do ambiente quebraria a rotação no cofre.
    """
    global _remote_secret_provider
    _remote_secret_provider = provider


def get_runtime_secret(key: str) -> str:
    """Busca o valor atual no cofre e usa o ambiente durante a transição.

    Não há cache de processo: backend, workers e notificadores observam uma
    rotação sem precisar reiniciar e sem manter cópias antigas indefinidamente.
    """
    normalized_key = key.strip().upper()
    if _remote_secret_provider is not None:
        try:
            return str(_remote_secret_provider(normalized_key) or "").strip()
        except Exception as exc:
            LOG.error(
                "Fonte remota de segredos indisponível para %s (%s).",
                normalized_key,
                type(exc).__name__,
            )
            raise RuntimeError(
                f"Não foi possível obter o segredo operacional {normalized_key}."
            ) from exc
    try:
        settings = Settings.from_environment()
    except Exception as exc:
        LOG.warning(
            "Configuração do cofre indisponível para %s (%s); usando ambiente de transição.",
            normalized_key,
            type(exc).__name__,
        )
        return os.getenv(normalized_key, "").strip()
    try:
        with get_session_factory()() as session:
            stored = session.get(IntegrationSecret, normalized_key)
    except SQLAlchemyError as exc:
        # Durante bootstrap/indisponibilidade do PostgreSQL, o ambiente segue
        # como fallback explícito e observável, sem registrar o valor secreto.
        LOG.warning(
            "Cofre PostgreSQL indisponível para %s (%s); usando ambiente de transição.",
            normalized_key,
            type(exc).__name__,
        )
        return os.getenv(normalized_key, "").strip()
    if stored:
        try:
            return decrypt_integration_secret(stored, settings)
        except Exception as exc:
            # Uma linha existente é a fonte oficial. Cair silenciosamente para
            # um token antigo do .env faria uma rotação quebrada parecer válida.
            LOG.error(
                "Segredo %s existe, mas não pôde ser decifrado (%s).",
                normalized_key,
                type(exc).__name__,
            )
            raise RuntimeError(
                f"O segredo {normalized_key} está cadastrado, mas é inválido."
            ) from exc
    return os.getenv(normalized_key, "").strip()


def clear_secret_cache() -> None:
    """Compatibilidade para chamadores antigos; as leituras já são imediatas."""

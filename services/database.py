"""Validação compartilhada da conexão PostgreSQL de produção."""

from __future__ import annotations

import os


def require_postgres_url() -> str:
    """Impede fallback silencioso para arquivos SQLite em produção."""
    value = os.getenv("DATABASE_URL", "").strip()
    if value.startswith("postgres://"):
        return "postgresql+psycopg://" + value.removeprefix("postgres://")
    if value.startswith("postgresql://"):
        return "postgresql+psycopg://" + value.removeprefix("postgresql://")
    if not value.startswith("postgresql+psycopg://"):
        raise RuntimeError(
            "DATABASE_URL PostgreSQL não configurada. "
            "SQLite não é aceito no runtime."
        )
    return value

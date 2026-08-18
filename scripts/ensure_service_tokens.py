"""Garante tokens dedicados para processos internos sem imprimir seus valores."""

from __future__ import annotations

import argparse
import os
from datetime import UTC, datetime
from pathlib import Path

from dotenv import dotenv_values, load_dotenv, set_key
from sqlalchemy import select

from machine_admin.db import get_session_factory
from machine_admin.models import AdminUser, ApiToken
from machine_admin.security import hash_api_token
from machine_admin.services import issue_api_token


SERVICE_TOKENS = {
    "WORKER_API_TOKEN": ("system-workers", ["jobs:read", "workers:execute"]),
    "TELEGRAM_BACKEND_API_TOKEN": (
        "system-telegram-controller",
        ["jobs:read", "jobs:write"],
    ),
}


def _is_usable(token: ApiToken | None, required_scopes: list[str]) -> bool:
    if not token or token.revoked_at is not None:
        return False
    if token.expires_at is not None and token.expires_at <= datetime.now(UTC):
        return False
    return set(required_scopes).issubset(set(token.scopes))


def ensure_service_tokens(env_file: Path) -> list[str]:
    """Cria somente tokens ausentes/inválidos e persiste o bruto no env mestre."""
    load_dotenv(env_file, override=True, interpolate=False)
    values = dotenv_values(env_file, interpolate=False)
    actions: list[str] = []
    with get_session_factory()() as session:
        owner = session.scalar(
            select(AdminUser)
            .where(AdminUser.active.is_(True), AdminUser.role == "admin")
            .order_by(AdminUser.id)
        )
        if not owner:
            raise RuntimeError(
                "Nenhum administrador ativo disponível para ser dono dos tokens de serviço."
            )
        for env_key, (name, scopes) in SERVICE_TOKENS.items():
            raw = str(values.get(env_key) or "").strip()
            stored = (
                session.scalar(
                    select(ApiToken).where(ApiToken.token_hash == hash_api_token(raw))
                )
                if raw
                else None
            )
            if _is_usable(stored, scopes):
                actions.append(f"{env_key}: mantido")
                continue
            _, replacement = issue_api_token(
                session,
                owner_id=owner.id,
                name=name,
                scopes=scopes,
                expires_in_days=3650,
            )
            set_key(str(env_file), env_key, replacement, quote_mode="always")
            os.environ[env_key] = replacement
            actions.append(f"{env_key}: criado")
        session.commit()
    return actions


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", required=True, type=Path)
    args = parser.parse_args()
    if not args.env_file.is_file():
        raise SystemExit(f"Arquivo de ambiente ausente: {args.env_file}")
    for action in ensure_service_tokens(args.env_file):
        print(action)


if __name__ == "__main__":
    main()

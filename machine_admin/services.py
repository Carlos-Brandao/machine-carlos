"""Serviços de domínio usados pelas rotas administrativas."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from machine_admin.config import Settings
from machine_admin.models import (
    AdminUser,
    ApiToken,
    AuditLog,
    IntegrationSecret,
    Municipality,
    Platform,
    PortalCredential,
)
from machine_admin.security import (
    SecretCipher,
    generate_api_token,
    hash_password,
    verify_password,
)
from services.registry import MUNICIPALITIES, PLATFORMS


def sync_catalog(session: Session) -> None:
    for definition in PLATFORMS.values():
        platform = session.get(Platform, definition.slug) or Platform(slug=definition.slug)
        platform.name = definition.name
        platform.runner = definition.runner
        platform.start_hour = definition.start_hour
        platform.end_hour = definition.end_hour
        platform.enabled = definition.enabled
        session.add(platform)
    session.flush()
    for definition in MUNICIPALITIES.values():
        municipality = session.get(Municipality, definition.slug) or Municipality(
            slug=definition.slug
        )
        municipality.name = definition.name
        municipality.platform_slug = definition.platform_slug
        municipality.login_url = definition.login_url
        municipality.query_url = definition.query_url
        municipality.max_workers = definition.max_workers
        municipality.enabled = definition.enabled
        session.add(municipality)
    session.commit()


def bootstrap_admin(session: Session, settings: Settings) -> None:
    if not settings.bootstrap_admin_email or not settings.bootstrap_admin_password:
        return
    existing = session.scalar(
        select(AdminUser).where(AdminUser.email == settings.bootstrap_admin_email)
    )
    if existing:
        return
    session.add(
        AdminUser(
            email=settings.bootstrap_admin_email,
            display_name="Administrador",
            password_hash=hash_password(settings.bootstrap_admin_password),
            role="admin",
            active=True,
        )
    )
    session.commit()


def authenticate_admin(session: Session, email: str, password: str) -> AdminUser | None:
    user = session.scalar(select(AdminUser).where(AdminUser.email == email.strip().lower()))
    if not user or not user.active or not verify_password(user.password_hash, password):
        return None
    user.last_login_at = datetime.now(UTC)
    session.commit()
    return user


def create_admin_user(
    session: Session,
    *,
    email: str,
    display_name: str,
    password: str,
    role: str,
) -> AdminUser:
    email = email.strip().lower()
    display_name = display_name.strip()
    if "@" not in email or len(email) > 320:
        raise ValueError("E-mail administrativo inválido.")
    if not display_name:
        raise ValueError("Nome do usuário é obrigatório.")
    if role not in {"admin", "operator", "viewer"}:
        raise ValueError("Papel administrativo inválido.")
    if session.scalar(select(AdminUser.id).where(AdminUser.email == email)):
        raise ValueError("Já existe um usuário com esse e-mail.")
    user = AdminUser(
        email=email,
        display_name=display_name,
        password_hash=hash_password(password),
        role=role,
        active=True,
    )
    session.add(user)
    session.flush()
    return user


def issue_api_token(
    session: Session,
    *,
    owner_id: int,
    name: str,
    scopes: list[str],
) -> tuple[ApiToken, str]:
    name = name.strip()
    allowed_scopes = {"jobs:read", "jobs:write", "workers:execute"}
    normalized_scopes = sorted(set(scopes))
    if not name:
        raise ValueError("Nome do token é obrigatório.")
    if not normalized_scopes or not set(normalized_scopes) <= allowed_scopes:
        raise ValueError("Escopos do token são inválidos.")
    raw_token, prefix, token_hash = generate_api_token()
    token = ApiToken(
        owner_id=owner_id,
        name=name,
        token_prefix=prefix,
        token_hash=token_hash,
        scopes=normalized_scopes,
    )
    session.add(token)
    session.flush()
    return token, raw_token


def create_portal_credential(
    session: Session,
    settings: Settings,
    *,
    municipality_slug: str,
    label: str,
    username: str,
    password: str,
    consignataria: str | None = None,
) -> PortalCredential:
    municipality = session.get(Municipality, municipality_slug)
    if not municipality:
        raise ValueError("Convênio não encontrado.")
    label = label.strip()
    if not label or not username.strip() or not password:
        raise ValueError("Rótulo, usuário e senha são obrigatórios.")
    context_id = secrets.token_hex(16)
    cipher = SecretCipher(settings.master_key)
    credential = PortalCredential(
        municipality_slug=municipality_slug,
        label=label,
        encryption_context=context_id,
        username_ciphertext=cipher.encrypt(
            username, context=f"portal:{context_id}:username"
        ),
        password_ciphertext=cipher.encrypt(
            password, context=f"portal:{context_id}:password"
        ),
        status="active",
        max_parallel_sessions=1,
        settings_json=(
            {"consignataria": consignataria.strip()}
            if consignataria and consignataria.strip()
            else {}
        ),
    )
    session.add(credential)
    session.flush()
    return credential


def decrypt_portal_credential(
    credential: PortalCredential, settings: Settings
) -> tuple[str, str]:
    cipher = SecretCipher(settings.master_key)
    context_id = credential.encryption_context
    return (
        cipher.decrypt(
            credential.username_ciphertext,
            context=f"portal:{context_id}:username",
        ),
        cipher.decrypt(
            credential.password_ciphertext,
            context=f"portal:{context_id}:password",
        ),
    )


def upsert_integration_secret(
    session: Session,
    settings: Settings,
    *,
    key: str,
    value: str,
    description: str = "",
) -> IntegrationSecret:
    normalized_key = key.strip().upper()
    if not normalized_key or not normalized_key.replace("_", "").isalnum():
        raise ValueError("Nome de segredo inválido.")
    if not value:
        raise ValueError("O valor do segredo é obrigatório.")
    cipher = SecretCipher(settings.master_key)
    secret = session.get(IntegrationSecret, normalized_key) or IntegrationSecret(
        key=normalized_key,
        value_ciphertext=b"",
    )
    secret.value_ciphertext = cipher.encrypt(
        value, context=f"integration:{normalized_key}"
    )
    secret.description = description.strip() or None
    secret.rotated_at = datetime.now(UTC)
    session.add(secret)
    session.flush()
    return secret


def decrypt_integration_secret(
    secret: IntegrationSecret, settings: Settings
) -> str:
    return SecretCipher(settings.master_key).decrypt(
        secret.value_ciphertext, context=f"integration:{secret.key}"
    )


def audit(
    session: Session,
    *,
    actor_id: int | None,
    action: str,
    target_type: str,
    target_id: str | None = None,
    ip_address: str | None = None,
    details: dict | None = None,
) -> None:
    session.add(
        AuditLog(
            actor_id=actor_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            ip_address=ip_address,
            details=details or {},
        )
    )


def dashboard_counts(session: Session) -> dict[str, int]:
    from machine_admin.models import Dataset, Job, JobItem

    return {
        "users": session.scalar(select(func.count()).select_from(AdminUser)) or 0,
        "credentials": session.scalar(select(func.count()).select_from(PortalCredential)) or 0,
        "datasets": session.scalar(select(func.count()).select_from(Dataset)) or 0,
        "jobs_active": session.scalar(
            select(func.count()).select_from(Job).where(
                Job.status.in_(["awaiting_dataset", "queued", "running"])
            )
        ) or 0,
        "items_pending": session.scalar(
            select(func.count()).select_from(JobItem).where(
                JobItem.status.in_(["pending", "leased"])
            )
        ) or 0,
    }


def dashboard_robot_overview(session: Session) -> list[dict[str, object]]:
    """Resumo por convênio para a visão operacional do painel."""
    from machine_admin.models import Job

    municipalities = list(
        session.scalars(
            select(Municipality).order_by(Municipality.platform_slug, Municipality.name)
        )
    )
    credentials_by_municipality = dict(
        session.execute(
            select(PortalCredential.municipality_slug, func.count(PortalCredential.id))
            .where(PortalCredential.status == "active")
            .group_by(PortalCredential.municipality_slug)
        ).all()
    )
    active_statuses = ("awaiting_dataset", "queued", "running")
    active_jobs_by_municipality = dict(
        session.execute(
            select(Job.municipality_slug, func.count(Job.id))
            .where(Job.status.in_(active_statuses))
            .group_by(Job.municipality_slug)
        ).all()
    )
    return [
        {
            "municipality": municipality,
            "platform": session.get(Platform, municipality.platform_slug),
            "credentials": credentials_by_municipality.get(municipality.slug, 0),
            "active_jobs": active_jobs_by_municipality.get(municipality.slug, 0),
        }
        for municipality in municipalities
    ]

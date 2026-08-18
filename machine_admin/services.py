"""Serviços de domínio usados pelas rotas administrativas."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

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
    """Semeia o catálogo inicial sem substituir decisões operacionais do banco.

    Depois da primeira criação, URLs, prontidão, limites e versões passam a ser
    configuração persistente. Isso evita que um restart desfaça uma pausa ou
    uma correção realizada por operação/migração.
    """
    for definition in PLATFORMS.values():
        if session.get(Platform, definition.slug) is None:
            session.add(
                Platform(
                    slug=definition.slug,
                    name=definition.name,
                    runner=definition.runner,
                    start_hour=definition.start_hour,
                    end_hour=definition.end_hour,
                    enabled=definition.enabled,
                )
            )
    session.flush()
    for definition in MUNICIPALITIES.values():
        if session.get(Municipality, definition.slug) is None:
            session.add(
                Municipality(
                    slug=definition.slug,
                    name=definition.name,
                    platform_slug=definition.platform_slug,
                    login_url=definition.login_url,
                    query_url=definition.query_url,
                    max_workers=definition.max_workers,
                    enabled=definition.enabled,
                    operational_status=definition.operational_status,
                    timezone=definition.timezone,
                    input_schema=definition.input_schema,
                    schedule_policy=definition.schedule_policy,
                    adapter_version=definition.adapter_version,
                )
            )
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
    expires_in_days: int | None = 365,
) -> tuple[ApiToken, str]:
    name = name.strip()
    allowed_scopes = {"jobs:read", "jobs:write", "workers:execute"}
    normalized_scopes = sorted(set(scopes))
    if not name:
        raise ValueError("Nome do token é obrigatório.")
    if not normalized_scopes or not set(normalized_scopes) <= allowed_scopes:
        raise ValueError("Escopos do token são inválidos.")
    if expires_in_days is not None and not 1 <= expires_in_days <= 3650:
        raise ValueError("A validade deve ficar entre 1 e 3650 dias.")
    raw_token, prefix, token_hash = generate_api_token()
    token = ApiToken(
        owner_id=owner_id,
        name=name,
        token_prefix=prefix,
        token_hash=token_hash,
        scopes=normalized_scopes,
        expires_at=(
            datetime.now(UTC) + timedelta(days=expires_in_days)
            if expires_in_days is not None
            else None
        ),
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
    portal_profile: str | None = None,
) -> PortalCredential:
    municipality = session.get(Municipality, municipality_slug)
    if not municipality:
        raise ValueError("Convênio não encontrado.")
    label = label.strip()
    legacy_profile = (consignataria or "").strip() or None
    profile = (
        (portal_profile if portal_profile is not None else consignataria) or ""
    ).strip() or None
    if not label or not username.strip() or not password:
        raise ValueError("Rótulo, usuário e senha são obrigatórios.")
    if len(label) > 120:
        raise ValueError("A identificação deve ter no máximo 120 caracteres.")
    if profile and len(profile) > 160:
        raise ValueError("O perfil no portal deve ter no máximo 160 caracteres.")
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
        portal_username=username.strip(),
        portal_password=password,
        consignataria=legacy_profile or profile,
        portal_profile=profile,
        status="active",
        max_parallel_sessions=1,
        settings_json={
            "consignataria": legacy_profile or profile,
            "portal_profile": profile,
        },
    )
    session.add(credential)
    session.flush()
    return credential


def update_portal_credential(
    session: Session,
    settings: Settings,
    *,
    credential: PortalCredential,
    label: str,
    username: str,
    password: str,
    consignataria: str | None = None,
    portal_profile: str | None = None,
) -> PortalCredential:
    label = label.strip()
    if not label:
        raise ValueError("Identificação é obrigatória.")
    if len(label) > 120:
        raise ValueError("A identificação deve ter no máximo 120 caracteres.")
    credential.label = label
    access_changed = False
    profile_was_supplied = portal_profile is not None or consignataria is not None
    if profile_was_supplied:
        legacy_profile = (consignataria or "").strip() or None
        profile = (
            (portal_profile if portal_profile is not None else consignataria) or ""
        ).strip() or None
        if profile and len(profile) > 160:
            raise ValueError("O perfil no portal deve ter no máximo 160 caracteres.")
        access_changed = profile != credential.portal_profile
        credential.portal_profile = profile
        credential.consignataria = legacy_profile or profile
        credential.settings_json = {
            **credential.settings_json,
            "consignataria": legacy_profile or profile,
            "portal_profile": profile,
        }
    cipher = SecretCipher(settings.master_key)
    context_id = credential.encryption_context
    if username.strip():
        normalized_username = username.strip()
        access_changed = access_changed or normalized_username != credential.portal_username
        credential.portal_username = normalized_username
        credential.username_ciphertext = cipher.encrypt(
            normalized_username, context=f"portal:{context_id}:username"
        )
    if password:
        access_changed = access_changed or password != credential.portal_password
        credential.portal_password = password
        credential.password_ciphertext = cipher.encrypt(
            password, context=f"portal:{context_id}:password"
        )
    if access_changed:
        # Uma correção de usuário/senha/perfil precisa ser testada no próximo
        # lease; manter ``invalid`` ou cooldown tornaria a edição inócua.
        credential.status = "active"
        credential.failure_count = 0
        credential.cooldown_until = None
        credential.last_error = None
        credential.last_validated_at = None
    session.flush()
    return credential


def decrypt_portal_credential(
    credential: PortalCredential, settings: Settings
) -> tuple[str, str]:
    if credential.portal_username is not None and credential.portal_password is not None:
        return credential.portal_username, credential.portal_password
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


def migrate_portal_credentials_to_plaintext(session: Session, settings: Settings) -> None:
    """Preenche o armazenamento aberto para credenciais legadas cifradas."""
    for credential in session.scalars(
        select(PortalCredential).where(
            (PortalCredential.portal_username.is_(None))
            | (PortalCredential.portal_password.is_(None))
        )
    ):
        username, password = decrypt_portal_credential(credential, settings)
        credential.portal_username = username
        credential.portal_password = password


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

"""FastAPI do painel e das rotas operacionais."""

from __future__ import annotations

import hmac
import io
import json
import re
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from machine_admin.config import Settings
from machine_admin.datasets import create_job_for_dataset, delete_dataset_blob, import_dataset
from machine_admin.db import get_db, get_session_factory, get_settings
from machine_admin.exports import (
    build_job_export as build_job_export_file,
    job_export_filename,
)
from machine_admin.models import (
    AdminUser,
    ApiToken,
    AuditLog,
    ConsultationResult,
    CredentialLease,
    Dataset,
    DatasetRecord,
    IntegrationSecret,
    Job,
    JobEvent,
    JobItem,
    JobItemAttempt,
    Municipality,
    NotificationOutbox,
    Platform,
    PortalCredential,
    WorkerHeartbeat,
)
from machine_admin.notifications import enqueue_job_result
from machine_admin.queue import (
    RETRYABLE_OUTCOMES,
    acquire_credential,
    claim_job_items,
    complete_job_item,
    expire_exhausted_job_items,
    heartbeat_credential,
    requeue_job_item,
    release_credential,
)
from machine_admin.schemas import (
    AcquireCredentialRequest,
    BatchRequest,
    ClaimItemsRequest,
    CompleteItemRequest,
    CredentialReportRequest,
    RequeueItemRequest,
    WorkerRequest,
    WorkerStatusRequest,
)
from machine_admin.readiness import TRANSACTIONAL_ADAPTERS, assess_municipality
from machine_admin.scheduling import schedule_decision
from machine_admin.security import SecretCipher, hash_api_token, hash_password
from machine_admin.secret_store import clear_secret_cache, get_runtime_secret
from machine_admin.services import (
    audit,
    authenticate_admin,
    bootstrap_admin,
    create_admin_user,
    create_portal_credential,
    dashboard_counts,
    dashboard_robot_overview,
    decrypt_portal_credential,
    issue_api_token,
    migrate_portal_credentials_to_plaintext,
    sync_catalog,
    update_portal_credential,
    upsert_integration_secret,
)


PACKAGE_DIR = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))


@dataclass(frozen=True)
class ApiPrincipal:
    name: str
    scopes: frozenset[str]
    token_id: int | None = None


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    def login_rate_key(request: Request, email: str) -> tuple[str, str]:
        host = request.client.host if request.client else "unknown"
        return host[:64], email.strip().lower()[:254]

    def login_is_limited(session: Session, host: str, email: str) -> bool:
        threshold = datetime.now(UTC) - timedelta(minutes=15)
        base = (AuditLog.action == "login.failed", AuditLog.created_at >= threshold)
        account_failures = int(
            session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(*base, AuditLog.target_id == email[:120])
            )
            or 0
        )
        origin_failures = int(
            session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(*base, AuditLog.ip_address == host)
            )
            or 0
        )
        # Cinco falhas protegem uma conta; vinte protegem a origem contra a
        # troca infinita de e-mails. O estado é compartilhado no PostgreSQL.
        return account_failures >= 5 or origin_failures >= 20

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        with get_session_factory()() as session:
            sync_catalog(session)
            migrate_portal_credentials_to_plaintext(session, settings)
            session.commit()
            bootstrap_admin(session, settings)
        yield

    app = FastAPI(title="Machine Admin", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        session_cookie="machine_admin_session",
        max_age=12 * 60 * 60,
        same_site="strict",
        https_only=settings.cookie_secure,
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts))
    app.mount("/static", StaticFiles(directory=str(PACKAGE_DIR / "static")), name="static")

    def csrf_token(request: Request) -> str:
        token = request.session.get("csrf")
        if not token:
            token = secrets.token_urlsafe(32)
            request.session["csrf"] = token
        return str(token)

    def validate_csrf(request: Request, candidate: str) -> None:
        expected = str(request.session.get("csrf", ""))
        if not expected or not hmac.compare_digest(expected, candidate):
            raise HTTPException(status_code=403, detail="Token CSRF inválido.")

    def current_user(request: Request, session: Session) -> AdminUser | None:
        user_id = request.session.get("user_id")
        version = request.session.get("session_version")
        if not user_id:
            return None
        user = session.get(AdminUser, int(user_id))
        if not user or not user.active or user.session_version != version:
            request.session.clear()
            return None
        return user

    def page_context(
        request: Request,
        user: AdminUser | None,
        **values: object,
    ) -> dict[str, object]:
        flash = request.session.pop("flash", None)
        return {
            "request": request,
            "user": user,
            "csrf_token": csrf_token(request),
            "flash": flash,
            "now_utc": datetime.now(UTC),
            **values,
        }

    def require_browser_user(
        request: Request,
        session: Session,
        *,
        admin_only: bool = False,
        write_access: bool = False,
    ) -> AdminUser | RedirectResponse:
        user = current_user(request, session)
        if not user:
            return RedirectResponse("/login", status_code=303)
        if admin_only and user.role != "admin":
            raise HTTPException(status_code=403, detail="Acesso restrito a administradores.")
        if write_access and user.role not in {"admin", "operator"}:
            raise HTTPException(status_code=403, detail="Perfil sem permissão de escrita.")
        return user

    def client_ip(request: Request) -> str | None:
        return request.client.host if request.client else None

    def normalized_portal_url(value: str, field: str) -> str | None:
        cleaned = value.strip()
        if not cleaned:
            return None
        parsed = urlsplit(cleaned)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"{field} deve ser uma URL HTTP ou HTTPS completa.")
        return cleaned

    def normalized_hour(value: str, *, end: bool = False) -> int | None:
        cleaned = value.strip()
        if not cleaned:
            return None
        hour = int(cleaned)
        maximum = 24 if end else 23
        if hour < 0 or hour > maximum or (end and hour == 0):
            raise ValueError(
                f"Horário deve estar entre {'1 e 24' if end else '0 e 23'}."
            )
        return hour

    def validate_timezone(value: str) -> str:
        cleaned = value.strip() or "America/Fortaleza"
        try:
            ZoneInfo(cleaned)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("Fuso horário IANA inválido.") from exc
        return cleaned

    def api_principal(
        request: Request, session: Session = Depends(get_db)
    ) -> ApiPrincipal:
        header = request.headers.get("Authorization", "")
        scheme, _, raw_token = header.partition(" ")
        if scheme.lower() != "bearer" or not raw_token:
            raise HTTPException(status_code=401, detail="Token Bearer ausente.")
        token = session.scalar(
            select(ApiToken).where(
                ApiToken.token_hash == hash_api_token(raw_token),
                ApiToken.revoked_at.is_(None),
            )
        )
        now = datetime.now(UTC)
        owner = session.get(AdminUser, token.owner_id) if token else None
        if (
            not token
            or not owner
            or not owner.active
            or (token.expires_at and token.expires_at <= now)
        ):
            raise HTTPException(status_code=401, detail="Token inválido ou expirado.")
        # Observabilidade não deve transformar o token compartilhado do pool
        # em um hot row a cada heartbeat/item. Atualize no máximo a cada 5 min.
        usage_cutoff = now - timedelta(minutes=5)
        if token.last_used_at is None or token.last_used_at <= usage_cutoff:
            session.execute(
                update(ApiToken)
                .where(
                    ApiToken.id == token.id,
                    or_(
                        ApiToken.last_used_at.is_(None),
                        ApiToken.last_used_at <= usage_cutoff,
                    ),
                )
                .values(last_used_at=now)
            )
            session.commit()
        return ApiPrincipal(token.name, frozenset(token.scopes), token.id)

    def require_scope(scope: str):
        def dependency(principal: ApiPrincipal = Depends(api_principal)) -> ApiPrincipal:
            if "*" not in principal.scopes and scope not in principal.scopes:
                raise HTTPException(status_code=403, detail="Escopo insuficiente.")
            return principal

        return dependency

    @app.get("/api/runtime/secrets/{secret_key}")
    def api_runtime_secret(
        secret_key: str,
        principal: ApiPrincipal = Depends(api_principal),
    ):
        """Entrega somente segredos operacionais autorizados ao processo.

        A lista é fechada para que um token de worker nunca consiga extrair
        credenciais administrativas, chave mestre, banco ou outros valores do
        ambiente. A resposta não pode ser armazenada por proxies/navegadores.
        """
        normalized = secret_key.strip().upper()
        allowed_scopes = {
            "TWOCAPTCHA_API_KEY": frozenset({"workers:execute"}),
            "CONSIGX_HTTPS_PROXY": frozenset({"workers:execute"}),
            "SAFECONSIG_HTTP_PROXY": frozenset({"workers:execute"}),
            "TELEGRAM_BOT_TOKEN": frozenset({"jobs:write"}),
        }
        required = allowed_scopes.get(normalized)
        if not required or (
            "*" not in principal.scopes
            and principal.scopes.isdisjoint(required)
        ):
            # Não revele nem mesmo se uma chave fora do contrato existe.
            raise HTTPException(status_code=404, detail="Segredo indisponível.")
        try:
            value = get_runtime_secret(normalized)
        except RuntimeError as exc:
            raise HTTPException(
                status_code=503, detail="Cofre de segredos indisponível."
            ) from exc
        if not value:
            raise HTTPException(status_code=404, detail="Segredo não configurado.")
        return JSONResponse(
            {"key": normalized, "value": value},
            headers={"Cache-Control": "no-store, private", "Pragma": "no-cache"},
        )

    def job_execution_state(session: Session, job: Job) -> dict[str, object]:
        municipality = session.get(Municipality, job.municipality_slug)
        if not municipality:
            return {
                "executable": False,
                "reason": "Convênio não encontrado.",
                "issues": [{"code": "agreement_missing", "message": "Convênio não encontrado."}],
                "next_start_at": None,
            }
        platform = session.get(Platform, municipality.platform_slug)
        if not platform:
            return {
                "executable": False,
                "reason": "Processadora não encontrada.",
                "issues": [{"code": "processor_missing", "message": "Processadora não encontrada."}],
                "next_start_at": None,
            }
        if job.status not in {"queued", "running"}:
            labels = {
                "awaiting_dataset": "O job ainda não possui uma base.",
                "paused": "O job está pausado.",
                "completed": "O job foi concluído.",
                "completed_with_errors": "O job terminou com erros.",
                "blocked": "O job está bloqueado.",
                "failed": "O job falhou.",
                "cancelled": "O job foi interrompido.",
            }
            reason = labels.get(job.status, "O job não está executável.")
            return {
                "executable": False,
                "reason": reason,
                "issues": [
                    {
                        "code": f"job_{job.status}",
                        "message": reason,
                        "action": "Consulte o histórico e as ações disponíveis.",
                        "severity": "blocking",
                    }
                ],
                "next_start_at": None,
                "next_retry_at": None,
                "work_available": False,
            }
        readiness = assess_municipality(session, municipality)
        schedule = schedule_decision(municipality, platform)
        now = datetime.now(UTC)
        not_before_allowed = not job.not_before or job.not_before <= now
        ready_item = session.scalar(
            select(JobItem.id)
            .where(
                JobItem.job_id == job.id,
                JobItem.attempts < JobItem.max_attempts,
                or_(
                    (
                        (JobItem.status == "pending")
                        & or_(
                            JobItem.next_attempt_at.is_(None),
                            JobItem.next_attempt_at <= now,
                        )
                    ),
                    (
                        (JobItem.status == "leased")
                        & (JobItem.lease_expires_at <= now)
                    ),
                ),
            )
            .limit(1)
        )
        unfinished_item = session.scalar(
            select(JobItem.id)
            .where(
                JobItem.job_id == job.id,
                JobItem.status.in_(["pending", "leased"]),
            )
            .limit(1)
        )
        next_retry_at = session.scalar(
            select(func.min(JobItem.next_attempt_at)).where(
                JobItem.job_id == job.id,
                JobItem.status == "pending",
                JobItem.next_attempt_at > now,
            )
        )
        work_available = ready_item is not None
        issues = [issue.as_dict() for issue in readiness.issues]
        if not schedule.allowed:
            issues.append(
                {
                    "code": "outside_schedule",
                    "message": schedule.reason,
                    "action": "Aguarde a próxima janela configurada.",
                    "severity": "blocking",
                }
            )
        if not not_before_allowed:
            issues.append(
                {
                    "code": "not_before",
                    "message": "O job está aguardando o horário da próxima tentativa.",
                    "action": "Aguarde o horário indicado.",
                    "severity": "blocking",
                }
            )
        if not work_available:
            issues.append(
                {
                    "code": (
                        "items_waiting"
                        if unfinished_item is not None
                        else "items_unavailable"
                    ),
                    "message": (
                        "Os itens restantes estão em backoff ou já foram reservados "
                        "por outro worker."
                        if unfinished_item is not None
                        else "Não há item disponível para reservar."
                    ),
                    "action": (
                        "Aguarde a próxima tentativa; nenhum login será aberto agora."
                        if unfinished_item is not None
                        else "Atualize o job ou consulte seu histórico."
                    ),
                    "severity": "blocking",
                }
            )
        next_times = [
            value
            for value in (schedule.next_start_at, job.not_before, next_retry_at)
            if value is not None and value > now
        ]
        executable = (
            readiness.can_start
            and schedule.allowed
            and not_before_allowed
            and work_available
        )
        return {
            "executable": executable,
            "reason": "Pronto para execução." if executable else str(issues[0]["message"]),
            "issues": issues,
            "next_start_at": min(next_times).isoformat() if next_times else None,
            "next_retry_at": next_retry_at.isoformat() if next_retry_at else None,
            "work_available": work_available,
            "schedule": schedule.as_dict(),
            "readiness": readiness.as_dict(),
        }

    @app.get("/health")
    def health(session: Session = Depends(get_db)) -> dict[str, object]:
        session.execute(select(1))
        now = datetime.now(UTC)
        workers_online = session.scalar(
            select(func.count())
            .select_from(WorkerHeartbeat)
            .where(WorkerHeartbeat.expires_at > now)
        ) or 0
        notifications_pending = session.scalar(
            select(func.count())
            .select_from(NotificationOutbox)
            .where(NotificationOutbox.status.in_(["pending", "retry", "processing"]))
        ) or 0
        notifications_failed = session.scalar(
            select(func.count())
            .select_from(NotificationOutbox)
            .where(NotificationOutbox.status == "failed")
        ) or 0
        return {
            "ok": True,
            "status": "degraded" if notifications_failed else "healthy",
            "database": "postgresql",
            "workers_online": int(workers_online),
            "notifications_pending": int(notifications_pending),
            "notifications_failed": int(notifications_failed),
        }

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, session: Session = Depends(get_db)):
        if current_user(request, session):
            return RedirectResponse("/", status_code=303)
        return TEMPLATES.TemplateResponse(
            request=request,
            name="login.html",
            context=page_context(request, None, error=None),
        )

    @app.post("/login", response_class=HTMLResponse)
    def login(
        request: Request,
        email: str = Form(...),
        password: str = Form(...),
        csrf: str = Form(...),
        session: Session = Depends(get_db),
    ):
        validate_csrf(request, csrf)
        rate_host, rate_email = login_rate_key(request, email)
        if login_is_limited(session, rate_host, rate_email):
            return TEMPLATES.TemplateResponse(
                request=request,
                name="login.html",
                context=page_context(
                    request,
                    None,
                    error="Muitas tentativas. Aguarde 15 minutos e tente novamente.",
                ),
                status_code=429,
            )
        user = (
            authenticate_admin(session, rate_email, password)
            if len(email.strip()) <= 254 and len(password) <= 1024
            else None
        )
        if not user:
            audit(
                session,
                actor_id=None,
                action="login.failed",
                target_type="admin_user",
                target_id=rate_email[:120],
                ip_address=client_ip(request),
            )
            session.commit()
            return TEMPLATES.TemplateResponse(
                request=request,
                name="login.html",
                context=page_context(request, None, error="E-mail ou senha inválidos."),
                status_code=401,
            )
        request.session.clear()
        request.session.update(
            {"user_id": user.id, "session_version": user.session_version}
        )
        audit(
            session,
            actor_id=user.id,
            action="login",
            target_type="admin_user",
            target_id=str(user.id),
            ip_address=client_ip(request),
        )
        session.commit()
        return RedirectResponse("/", status_code=303)

    @app.post("/logout")
    def logout(
        request: Request,
        csrf: str = Form(...),
        session: Session = Depends(get_db),
    ):
        validate_csrf(request, csrf)
        user = current_user(request, session)
        if user:
            audit(
                session,
                actor_id=user.id,
                action="logout",
                target_type="admin_user",
                target_id=str(user.id),
                ip_address=client_ip(request),
            )
            session.commit()
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, session: Session = Depends(get_db)):
        user = require_browser_user(request, session)
        if isinstance(user, RedirectResponse):
            return user
        jobs = list(session.scalars(select(Job).order_by(Job.created_at.desc()).limit(12)))
        robots = dashboard_robot_overview(session)
        for robot in robots:
            municipality = robot["municipality"]
            robot["readiness"] = assess_municipality(
                session, municipality
            ).as_dict()
        return TEMPLATES.TemplateResponse(
            request=request,
            name="dashboard.html",
            context=page_context(
                request,
                user,
                counts=dashboard_counts(session),
                jobs=jobs,
                robots=robots,
            ),
        )

    @app.get("/admin/agreements", response_class=HTMLResponse)
    def agreements_page(request: Request, session: Session = Depends(get_db)):
        user = require_browser_user(request, session)
        if isinstance(user, RedirectResponse):
            return user
        platforms = list(session.scalars(select(Platform).order_by(Platform.name)))
        municipalities = list(
            session.scalars(select(Municipality).order_by(Municipality.name))
        )
        reports = {
            item.slug: assess_municipality(session, item).as_dict()
            for item in municipalities
        }
        schedules = {
            item.slug: schedule_decision(item, item.platform).as_dict()
            for item in municipalities
            if item.platform
        }
        adapter_states = {
            item.slug: item.runner in TRANSACTIONAL_ADAPTERS for item in platforms
        }
        return TEMPLATES.TemplateResponse(
            request=request,
            name="agreements.html",
            context=page_context(
                request,
                user,
                platforms=platforms,
                municipalities=municipalities,
                reports=reports,
                schedules=schedules,
                adapter_states=adapter_states,
            ),
        )

    @app.post("/admin/processors/{platform_slug}")
    def update_processor(
        platform_slug: str,
        request: Request,
        name: str = Form(...),
        start_hour: int = Form(...),
        end_hour: int = Form(...),
        enabled: str | None = Form(None),
        csrf: str = Form(...),
        session: Session = Depends(get_db),
    ):
        user = require_browser_user(request, session, admin_only=True)
        if isinstance(user, RedirectResponse):
            return user
        validate_csrf(request, csrf)
        platform = session.get(Platform, platform_slug)
        if not platform:
            raise HTTPException(status_code=404, detail="Processadora não encontrada.")
        try:
            cleaned_name = name.strip()
            if not cleaned_name:
                raise ValueError("O nome da processadora é obrigatório.")
            if not (0 <= start_hour <= 23 and 1 <= end_hour <= 24):
                raise ValueError("A janela padrão deve usar horas entre 0 e 24.")
            if start_hour >= end_hour:
                raise ValueError("O início da janela deve ser anterior ao fim.")
            platform.name = cleaned_name[:120]
            platform.start_hour = start_hour
            platform.end_hour = end_hour
            platform.enabled = enabled == "on"
            audit(
                session,
                actor_id=user.id,
                action="processor.updated",
                target_type="platform",
                target_id=platform.slug,
                ip_address=client_ip(request),
                details={
                    "enabled": platform.enabled,
                    "window": [start_hour, end_hour],
                },
            )
            session.commit()
            request.session["flash"] = {
                "level": "success",
                "message": f"Processadora {platform.name} atualizada.",
            }
        except ValueError as exc:
            session.rollback()
            request.session["flash"] = {"level": "error", "message": str(exc)}
        return RedirectResponse("/admin/agreements", status_code=303)

    @app.post("/admin/agreements")
    def create_agreement(
        request: Request,
        slug: str = Form(...),
        name: str = Form(...),
        platform_slug: str = Form(...),
        csrf: str = Form(...),
        session: Session = Depends(get_db),
    ):
        user = require_browser_user(request, session, admin_only=True)
        if isinstance(user, RedirectResponse):
            return user
        validate_csrf(request, csrf)
        normalized_slug = slug.strip().lower()
        try:
            if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", normalized_slug):
                raise ValueError("Identificador inválido; use letras, números e hífens.")
            if session.get(Municipality, normalized_slug):
                raise ValueError("Já existe um convênio com esse identificador.")
            platform = session.get(Platform, platform_slug)
            if not platform:
                raise ValueError("Processadora inválida.")
            cleaned_name = name.strip()
            if not cleaned_name:
                raise ValueError("O nome do convênio é obrigatório.")
            municipality = Municipality(
                slug=normalized_slug,
                name=cleaned_name[:160],
                platform_slug=platform.slug,
                enabled=True,
                operational_status="draft",
                timezone="America/Fortaleza",
                max_workers=1,
                input_schema={
                    "version": 1,
                    "required": ["cpf"],
                    "optional": ["registration"],
                    "deduplication_key": ["cpf"],
                },
                schedule_policy={
                    "weekdays": [0, 1, 2, 3, 4],
                    "start_hour": None,
                    "end_hour": None,
                },
                settings_json={},
            )
            session.add(municipality)
            audit(
                session,
                actor_id=user.id,
                action="agreement.created",
                target_type="municipality",
                target_id=normalized_slug,
                ip_address=client_ip(request),
                details={"platform": platform.slug},
            )
            session.commit()
            request.session["flash"] = {
                "level": "success",
                "message": "Convênio criado como Rascunho. Complete o checklist antes de liberá-lo.",
            }
        except (IntegrityError, ValueError) as exc:
            session.rollback()
            message = str(exc) if isinstance(exc, ValueError) else "Não foi possível criar o convênio."
            request.session["flash"] = {"level": "error", "message": message}
        return RedirectResponse("/admin/agreements", status_code=303)

    @app.post("/admin/agreements/{municipality_slug}")
    def update_agreement(
        municipality_slug: str,
        request: Request,
        name: str = Form(...),
        operational_status: str = Form(...),
        timezone: str = Form(...),
        login_url: str = Form(""),
        query_url: str = Form(""),
        adapter_version: str = Form(""),
        max_workers: int = Form(...),
        start_hour: str = Form(""),
        end_hour: str = Form(""),
        weekdays: list[int] = Form(default=[]),
        registration_required: str | None = Form(None),
        enabled: str | None = Form(None),
        csrf: str = Form(...),
        session: Session = Depends(get_db),
    ):
        user = require_browser_user(request, session, admin_only=True)
        if isinstance(user, RedirectResponse):
            return user
        validate_csrf(request, csrf)
        municipality = session.get(Municipality, municipality_slug)
        if not municipality:
            raise HTTPException(status_code=404, detail="Convênio não encontrado.")
        try:
            states = {"draft", "testing", "ready", "degraded", "paused", "retired"}
            if operational_status not in states:
                raise ValueError("Estado operacional inválido.")
            cleaned_name = name.strip()
            if not cleaned_name:
                raise ValueError("O nome do convênio é obrigatório.")
            if max_workers < 1 or max_workers > 20:
                raise ValueError("A concorrência deve ficar entre 1 e 20 workers.")
            start = normalized_hour(start_hour)
            end = normalized_hour(end_hour, end=True)
            if start is not None and end is not None and start >= end:
                raise ValueError("O início da janela deve ser anterior ao fim.")
            days = sorted({int(value) for value in weekdays if 0 <= int(value) <= 6})
            if not days:
                raise ValueError("Selecione pelo menos um dia de execução.")

            required = ["cpf"]
            optional: list[str] = []
            duplicate_key = ["cpf"]
            if registration_required == "on":
                required.append("registration")
                duplicate_key.append("registration")
            else:
                optional.append("registration")

            municipality.name = cleaned_name[:160]
            municipality.enabled = enabled == "on"
            municipality.operational_status = operational_status
            municipality.timezone = validate_timezone(timezone)
            municipality.login_url = normalized_portal_url(login_url, "URL de login")
            municipality.query_url = normalized_portal_url(query_url, "URL de consulta")
            municipality.adapter_version = adapter_version.strip()[:64] or None
            municipality.max_workers = max_workers
            municipality.input_schema = {
                "version": 1,
                "required": required,
                "optional": optional,
                "deduplication_key": duplicate_key,
            }
            municipality.schedule_policy = {
                "weekdays": days,
                "start_hour": start,
                "end_hour": end,
            }
            session.flush()

            if operational_status == "ready":
                report = assess_municipality(
                    session, municipality, require_online_worker=False
                )
                if not report.can_start:
                    reasons = "; ".join(issue.message for issue in report.issues[:3])
                    raise ValueError(f"Não foi possível marcar como Pronto: {reasons}")
            audit(
                session,
                actor_id=user.id,
                action="agreement.updated",
                target_type="municipality",
                target_id=municipality.slug,
                ip_address=client_ip(request),
                details={
                    "status": municipality.operational_status,
                    "enabled": municipality.enabled,
                    "max_workers": municipality.max_workers,
                    "required": required,
                    "schedule": municipality.schedule_policy,
                },
            )
            session.commit()
            request.session["flash"] = {
                "level": "success",
                "message": f"Regras de {municipality.name} atualizadas.",
            }
        except (ValueError, ZoneInfoNotFoundError) as exc:
            session.rollback()
            request.session["flash"] = {"level": "error", "message": str(exc)}
        return RedirectResponse("/admin/agreements", status_code=303)

    @app.get("/admin/users", response_class=HTMLResponse)
    def users_page(request: Request, session: Session = Depends(get_db)):
        user = require_browser_user(request, session, admin_only=True)
        if isinstance(user, RedirectResponse):
            return user
        users = list(session.scalars(select(AdminUser).order_by(AdminUser.email)))
        return TEMPLATES.TemplateResponse(
            request=request,
            name="users.html",
            context=page_context(request, user, users=users),
        )

    @app.post("/admin/users")
    def add_user(
        request: Request,
        email: str = Form(...),
        display_name: str = Form(...),
        password: str = Form(...),
        role: str = Form(...),
        csrf: str = Form(...),
        session: Session = Depends(get_db),
    ):
        user = require_browser_user(request, session, admin_only=True)
        if isinstance(user, RedirectResponse):
            return user
        validate_csrf(request, csrf)
        try:
            created = create_admin_user(
                session,
                email=email,
                display_name=display_name,
                password=password,
                role=role,
            )
            audit(
                session,
                actor_id=user.id,
                action="admin_user.created",
                target_type="admin_user",
                target_id=str(created.id),
                ip_address=client_ip(request),
            )
            session.commit()
            request.session["flash"] = {"level": "success", "message": "Usuário criado."}
        except ValueError as exc:
            session.rollback()
            request.session["flash"] = {"level": "error", "message": str(exc)}
        except IntegrityError:
            session.rollback()
            request.session["flash"] = {
                "level": "error",
                "message": "Já existe um usuário com estes dados.",
            }
        return RedirectResponse("/admin/users", status_code=303)

    def active_admin_count(session: Session) -> int:
        return int(
            session.scalar(
                select(func.count())
                .select_from(AdminUser)
                .where(AdminUser.role == "admin", AdminUser.active.is_(True))
            )
            or 0
        )

    def lock_admin_invariant(session: Session) -> None:
        # Toda alteração que pode remover um administrador usa a mesma ordem
        # de locks. Isso elimina TOCTOU e evita o cenário de zero admins.
        list(
            session.scalars(
                select(AdminUser)
                .where(AdminUser.role == "admin")
                .order_by(AdminUser.id)
                .with_for_update()
            )
        )

    @app.post("/admin/users/{user_id}/toggle")
    def toggle_user(
        user_id: int,
        request: Request,
        csrf: str = Form(...),
        session: Session = Depends(get_db),
    ):
        actor = require_browser_user(request, session, admin_only=True)
        if isinstance(actor, RedirectResponse):
            return actor
        validate_csrf(request, csrf)
        lock_admin_invariant(session)
        target = session.scalar(
            select(AdminUser).where(AdminUser.id == user_id).with_for_update()
        )
        if not target:
            raise HTTPException(status_code=404, detail="Usuário não encontrado.")
        if target.id == actor.id and target.active:
            request.session["flash"] = {
                "message": "Você não pode desativar a própria conta.",
                "level": "warning",
            }
            return RedirectResponse("/admin/users", status_code=303)
        if target.active and target.role == "admin" and active_admin_count(session) <= 1:
            request.session["flash"] = {
                "message": "O último administrador ativo não pode ser desativado.",
                "level": "warning",
            }
            return RedirectResponse("/admin/users", status_code=303)
        target.active = not target.active
        target.session_version += 1
        if not target.active:
            now = datetime.now(UTC)
            session.execute(
                update(ApiToken)
                .where(ApiToken.owner_id == target.id, ApiToken.revoked_at.is_(None))
                .values(revoked_at=now)
            )
        audit(
            session,
            actor_id=actor.id,
            action="admin_user.activated" if target.active else "admin_user.disabled",
            target_type="admin_user",
            target_id=str(target.id),
            ip_address=client_ip(request),
        )
        session.commit()
        request.session["flash"] = {
            "message": "Usuário ativado." if target.active else "Usuário desativado e sessões invalidadas.",
            "level": "success",
        }
        return RedirectResponse("/admin/users", status_code=303)

    @app.post("/admin/users/{user_id}/role")
    def change_user_role(
        user_id: int,
        request: Request,
        role: str = Form(...),
        csrf: str = Form(...),
        session: Session = Depends(get_db),
    ):
        actor = require_browser_user(request, session, admin_only=True)
        if isinstance(actor, RedirectResponse):
            return actor
        validate_csrf(request, csrf)
        lock_admin_invariant(session)
        target = session.scalar(
            select(AdminUser).where(AdminUser.id == user_id).with_for_update()
        )
        if not target or role not in {"admin", "operator", "viewer"}:
            raise HTTPException(status_code=400, detail="Usuário ou perfil inválido.")
        if target.role == "admin" and role != "admin" and active_admin_count(session) <= 1:
            request.session["flash"] = {
                "message": "O último administrador não pode perder esse perfil.",
                "level": "warning",
            }
            return RedirectResponse("/admin/users", status_code=303)
        target.role = role
        target.session_version += 1
        audit(
            session,
            actor_id=actor.id,
            action="admin_user.role_changed",
            target_type="admin_user",
            target_id=str(target.id),
            ip_address=client_ip(request),
            details={"role": role},
        )
        session.commit()
        request.session["flash"] = {"message": "Perfil atualizado.", "level": "success"}
        return RedirectResponse("/admin/users", status_code=303)

    @app.post("/admin/users/{user_id}/reset-password")
    def reset_user_password(
        user_id: int,
        request: Request,
        password: str = Form(...),
        csrf: str = Form(...),
        session: Session = Depends(get_db),
    ):
        actor = require_browser_user(request, session, admin_only=True)
        if isinstance(actor, RedirectResponse):
            return actor
        validate_csrf(request, csrf)
        target = session.get(AdminUser, user_id)
        if not target:
            raise HTTPException(status_code=404, detail="Usuário não encontrado.")
        try:
            target.password_hash = hash_password(password)
        except ValueError as exc:
            request.session["flash"] = {"message": str(exc), "level": "error"}
            return RedirectResponse("/admin/users", status_code=303)
        target.session_version += 1
        audit(
            session,
            actor_id=actor.id,
            action="admin_user.password_reset",
            target_type="admin_user",
            target_id=str(target.id),
            ip_address=client_ip(request),
        )
        session.commit()
        request.session["flash"] = {
            "message": "Senha redefinida e sessões anteriores encerradas.",
            "level": "success",
        }
        return RedirectResponse("/admin/users", status_code=303)

    @app.get("/admin/tokens", response_class=HTMLResponse)
    def tokens_page(request: Request, session: Session = Depends(get_db)):
        user = require_browser_user(request, session, admin_only=True)
        if isinstance(user, RedirectResponse):
            return user
        tokens = list(session.scalars(select(ApiToken).order_by(ApiToken.created_at.desc())))
        return TEMPLATES.TemplateResponse(
            request=request,
            name="tokens.html",
            context=page_context(request, user, tokens=tokens, raw_token=None),
        )

    @app.post("/admin/tokens")
    def add_token(
        request: Request,
        name: str = Form(...),
        scopes: str = Form("jobs:read,jobs:write"),
        expires_in_days: int = Form(365),
        csrf: str = Form(...),
        session: Session = Depends(get_db),
    ):
        user = require_browser_user(request, session, admin_only=True)
        if isinstance(user, RedirectResponse):
            return user
        validate_csrf(request, csrf)
        try:
            token, raw_token = issue_api_token(
                session,
                owner_id=user.id,
                name=name,
                scopes=[scope.strip() for scope in scopes.split(",") if scope.strip()],
                expires_in_days=expires_in_days,
            )
            audit(
                session,
                actor_id=user.id,
                action="api_token.created",
                target_type="api_token",
                target_id=str(token.id),
                ip_address=client_ip(request),
                details={"scopes": token.scopes},
            )
            session.commit()
            tokens = list(
                session.scalars(select(ApiToken).order_by(ApiToken.created_at.desc()))
            )
            return TEMPLATES.TemplateResponse(
                request=request,
                name="tokens.html",
                context=page_context(request, user, tokens=tokens, raw_token=raw_token),
            )
        except ValueError as exc:
            session.rollback()
            request.session["flash"] = {"level": "error", "message": str(exc)}
            return RedirectResponse("/admin/tokens", status_code=303)

    @app.post("/admin/tokens/{token_id}/revoke")
    def revoke_token(
        token_id: int,
        request: Request,
        csrf: str = Form(...),
        session: Session = Depends(get_db),
    ):
        user = require_browser_user(request, session, admin_only=True)
        if isinstance(user, RedirectResponse):
            return user
        validate_csrf(request, csrf)
        token = session.get(ApiToken, token_id)
        if token:
            token.revoked_at = datetime.now(UTC)
            audit(
                session,
                actor_id=user.id,
                action="api_token.revoked",
                target_type="api_token",
                target_id=str(token_id),
                ip_address=client_ip(request),
            )
            session.commit()
        return RedirectResponse("/admin/tokens", status_code=303)

    @app.get("/admin/credentials", response_class=HTMLResponse)
    def credentials_page(request: Request, session: Session = Depends(get_db)):
        user = require_browser_user(request, session)
        if isinstance(user, RedirectResponse):
            return user
        credentials = list(
            session.scalars(select(PortalCredential).order_by(PortalCredential.id))
        )
        municipalities = list(
            session.scalars(
                select(Municipality)
                .where(Municipality.enabled.is_(True))
                .order_by(Municipality.name)
            )
        )
        return TEMPLATES.TemplateResponse(
            request=request,
            name="credentials.html",
            context=page_context(
                request,
                user,
                credentials=credentials,
                municipalities=municipalities,
                municipality_map={item.slug: item for item in municipalities},
            ),
        )

    @app.get("/admin/credentials/{credential_id}/edit", response_class=HTMLResponse)
    def edit_credential_page(credential_id: int, request: Request, session: Session = Depends(get_db)):
        user = require_browser_user(request, session, admin_only=True)
        if isinstance(user, RedirectResponse):
            return user
        credential = session.get(PortalCredential, credential_id)
        if not credential:
            raise HTTPException(status_code=404, detail="Credencial não encontrada.")
        username, password = decrypt_portal_credential(credential, settings)
        return TEMPLATES.TemplateResponse(
            request=request,
            name="credential_edit.html",
            context=page_context(
                request,
                user,
                credential=credential,
                municipality=session.get(Municipality, credential.municipality_slug),
                username=username,
                password=password,
            ),
            headers={"Cache-Control": "no-store, private"},
        )

    @app.post("/admin/credentials/{credential_id}/edit")
    def edit_credential(credential_id: int, request: Request, label: str = Form(...), username: str = Form(""), password: str = Form(""), consignataria: str = Form(""), csrf: str = Form(...), session: Session = Depends(get_db)):
        user = require_browser_user(request, session, admin_only=True)
        if isinstance(user, RedirectResponse):
            return user
        validate_csrf(request, csrf)
        credential = session.get(PortalCredential, credential_id)
        if not credential:
            raise HTTPException(status_code=404, detail="Credencial não encontrada.")
        try:
            update_portal_credential(
                session,
                settings,
                credential=credential,
                label=label,
                username=username,
                password=password,
                portal_profile=consignataria,
            )
            audit(session, actor_id=user.id, action="portal_credential.updated", target_type="portal_credential", target_id=str(credential.id), ip_address=client_ip(request))
            session.commit()
            request.session["flash"] = {
                "level": "success",
                "message": "Credencial atualizada.",
            }
        except ValueError as exc:
            session.rollback()
            request.session["flash"] = {"level": "error", "message": str(exc)}
        except (IntegrityError, DataError):
            session.rollback()
            request.session["flash"] = {
                "level": "error",
                "message": "Já existe uma credencial com este rótulo ou os dados excedem o limite permitido.",
            }
        return RedirectResponse("/admin/credentials", status_code=303)

    @app.post("/admin/credentials")
    def add_credential(
        request: Request,
        municipality_slug: str = Form(...),
        label: str = Form(...),
        username: str = Form(...),
        password: str = Form(...),
        consignataria: str = Form(""),
        csrf: str = Form(...),
        session: Session = Depends(get_db),
    ):
        user = require_browser_user(request, session, admin_only=True)
        if isinstance(user, RedirectResponse):
            return user
        validate_csrf(request, csrf)
        try:
            credential = create_portal_credential(
                session,
                settings,
                municipality_slug=municipality_slug,
                label=label,
                username=username,
                password=password,
                portal_profile=consignataria,
            )
            audit(
                session,
                actor_id=user.id,
                action="portal_credential.created",
                target_type="portal_credential",
                target_id=str(credential.id),
                ip_address=client_ip(request),
            )
            session.commit()
            request.session["flash"] = {
                "level": "success",
                "message": "Credencial armazenada.",
            }
        except ValueError as exc:
            session.rollback()
            request.session["flash"] = {"level": "error", "message": str(exc)}
        except (IntegrityError, DataError):
            session.rollback()
            request.session["flash"] = {
                "level": "error",
                "message": "Já existe uma credencial com este rótulo ou os dados excedem o limite permitido.",
            }
        return RedirectResponse("/admin/credentials", status_code=303)

    @app.post("/admin/credentials/{credential_id}/toggle")
    def toggle_credential(
        credential_id: int,
        request: Request,
        csrf: str = Form(...),
        session: Session = Depends(get_db),
    ):
        user = require_browser_user(request, session, admin_only=True)
        if isinstance(user, RedirectResponse):
            return user
        validate_csrf(request, csrf)
        credential = session.get(PortalCredential, credential_id)
        if credential:
            if credential.status == "active":
                credential.status = "disabled"
            else:
                credential.status = "active"
                credential.failure_count = 0
                credential.cooldown_until = None
                credential.last_error = None
            audit(
                session,
                actor_id=user.id,
                action="portal_credential.toggled",
                target_type="portal_credential",
                target_id=str(credential.id),
                ip_address=client_ip(request),
                details={"status": credential.status},
            )
            session.commit()
        return RedirectResponse("/admin/credentials", status_code=303)

    @app.get("/admin/secrets", response_class=HTMLResponse)
    def secrets_page(request: Request, session: Session = Depends(get_db)):
        user = require_browser_user(request, session, admin_only=True)
        if isinstance(user, RedirectResponse):
            return user
        integration_secrets = list(
            session.scalars(select(IntegrationSecret).order_by(IntegrationSecret.key))
        )
        return TEMPLATES.TemplateResponse(
            request=request,
            name="secrets.html",
            context=page_context(request, user, integration_secrets=integration_secrets),
        )

    @app.post("/admin/secrets")
    def save_secret(
        request: Request,
        key: str = Form(...),
        value: str = Form(...),
        description: str = Form(""),
        csrf: str = Form(...),
        session: Session = Depends(get_db),
    ):
        user = require_browser_user(request, session, admin_only=True)
        if isinstance(user, RedirectResponse):
            return user
        validate_csrf(request, csrf)
        try:
            stored = upsert_integration_secret(
                session, settings, key=key, value=value, description=description
            )
            audit(
                session,
                actor_id=user.id,
                action="integration_secret.rotated",
                target_type="integration_secret",
                target_id=stored.key,
                ip_address=client_ip(request),
            )
            session.commit()
            clear_secret_cache()
            request.session["flash"] = {
                "level": "success",
                "message": "Segredo salvo; o valor não será exibido novamente.",
            }
        except ValueError as exc:
            session.rollback()
            request.session["flash"] = {"level": "error", "message": str(exc)}
        return RedirectResponse("/admin/secrets", status_code=303)

    @app.get("/admin/datasets", response_class=HTMLResponse)
    def datasets_page(request: Request, session: Session = Depends(get_db)):
        user = require_browser_user(request, session)
        if isinstance(user, RedirectResponse):
            return user
        datasets = list(session.scalars(select(Dataset).order_by(Dataset.created_at.desc())))
        municipalities = list(
            session.scalars(
                select(Municipality)
                .where(Municipality.enabled.is_(True))
                .order_by(Municipality.name)
            )
        )
        return TEMPLATES.TemplateResponse(
            request=request,
            name="datasets.html",
            context=page_context(
                request,
                user,
                datasets=datasets,
                municipalities=municipalities,
            ),
        )

    @app.post("/admin/datasets")
    def upload_dataset(
        request: Request,
        municipality_slug: str = Form(...),
        display_name: str = Form(""),
        duplicate_policy: str = Form("keep_first"),
        csrf: str = Form(...),
        file: UploadFile = File(...),
        session: Session = Depends(get_db),
    ):
        user = require_browser_user(request, session, write_access=True)
        if isinstance(user, RedirectResponse):
            return user
        validate_csrf(request, csrf)
        # A rota síncrona é executada pelo FastAPI no threadpool. Isso impede
        # que pandas/openpyxl e a persistência da base bloqueiem o event loop,
        # os heartbeats e as chamadas dos workers.
        payload = file.file.read(settings.max_upload_bytes + 1)
        dataset = None
        actor_id = user.id
        try:
            dataset = import_dataset(
                session,
                settings,
                municipality_slug=municipality_slug,
                filename=file.filename or "base.xlsx",
                payload=payload,
                uploaded_by_id=user.id,
                display_name=display_name or None,
                duplicate_policy=duplicate_policy,
            )
            audit(
                session,
                actor_id=user.id,
                action="dataset.imported",
                target_type="dataset",
                target_id=str(dataset.id),
                ip_address=client_ip(request),
                details={
                    "rows": dataset.row_count,
                    "municipality": municipality_slug,
                },
            )
            session.commit()
            request.session["flash"] = {
                "level": "warning" if dataset.error_message else "success",
                "message": (
                    f"Base importada com {dataset.row_count} registros. "
                    f"{dataset.error_message or ''}"
                ).strip(),
            }
        except ValueError as exc:
            storage_path = dataset.storage_path if dataset else None
            session.rollback()
            delete_dataset_blob(storage_path)
            audit(
                session,
                actor_id=actor_id,
                action="dataset.import_rejected",
                target_type="municipality",
                target_id=municipality_slug,
                ip_address=client_ip(request),
                details={"reason": str(exc)[:500], "filename": (file.filename or "")[:255]},
            )
            session.commit()
            request.session["flash"] = {"level": "error", "message": str(exc)}
        except IntegrityError:
            storage_path = dataset.storage_path if dataset else None
            session.rollback()
            delete_dataset_blob(storage_path)
            audit(
                session,
                actor_id=actor_id,
                action="dataset.import_rejected",
                target_type="municipality",
                target_id=municipality_slug,
                ip_address=client_ip(request),
                details={"reason": "A base conflita com um registro já existente.", "filename": (file.filename or "")[:255]},
            )
            session.commit()
            request.session["flash"] = {
                "level": "error",
                "message": "A base conflita com um registro já existente.",
            }
        return RedirectResponse("/admin/datasets", status_code=303)

    @app.post("/admin/datasets/{dataset_id}/jobs")
    def create_dataset_job(
        dataset_id: int,
        request: Request,
        csrf: str = Form(...),
        session: Session = Depends(get_db),
    ):
        user = require_browser_user(request, session, write_access=True)
        if isinstance(user, RedirectResponse):
            return user
        validate_csrf(request, csrf)
        dataset = session.get(Dataset, dataset_id)
        if not dataset or dataset.status != "ready":
            request.session["flash"] = {
                "message": "Base não encontrada ou indisponível para consulta.",
                "level": "error",
            }
            return RedirectResponse("/admin/datasets", status_code=303)
        municipality = session.scalar(
            select(Municipality)
            .where(Municipality.slug == dataset.municipality_slug)
            .with_for_update()
        )
        if not municipality:
            request.session["flash"] = {
                "message": "O convênio desta base não existe mais.",
                "level": "error",
            }
            return RedirectResponse("/admin/datasets", status_code=303)
        readiness = assess_municipality(session, municipality)
        if not readiness.can_start:
            request.session["flash"] = {
                "message": f"Job não iniciado: {readiness.summary}",
                "level": "warning",
            }
            return RedirectResponse("/admin/datasets", status_code=303)
        active_job = session.scalar(
            select(Job.id).where(
                Job.municipality_slug == municipality.slug,
                Job.status.in_(["queued", "running", "paused", "blocked"]),
            )
        )
        if active_job:
            request.session["flash"] = {
                "message": f"O convênio já possui o job #{active_job} ativo.",
                "level": "warning",
            }
            return RedirectResponse("/admin/jobs", status_code=303)
        job = create_job_for_dataset(session, dataset=dataset, requested_by_id=user.id)
        audit(
            session,
            actor_id=user.id,
            action="job.created_from_dataset",
            target_type="automation_job",
            target_id=str(job.id),
            ip_address=client_ip(request),
            details={"dataset_id": dataset.id, "municipality": dataset.municipality_slug},
        )
        session.commit()
        request.session["flash"] = {
            "message": f"Job #{job.id} criado a partir da base #{dataset.id}.",
            "level": "success",
        }
        return RedirectResponse("/admin/jobs", status_code=303)

    @app.get("/admin/jobs", response_class=HTMLResponse)
    def jobs_page(request: Request, session: Session = Depends(get_db)):
        user = require_browser_user(request, session)
        if isinstance(user, RedirectResponse):
            return user
        jobs = list(session.scalars(select(Job).order_by(Job.created_at.desc()).limit(200)))
        job_ids = [job.id for job in jobs]
        retryable_pending_by_job: dict[int, int] = {}
        retryable_failed_by_job: dict[int, int] = {}
        if job_ids:
            retryable_rows = session.execute(
                select(JobItem.job_id, JobItem.status, func.count(JobItem.id))
                .where(
                    JobItem.job_id.in_(job_ids),
                    JobItem.outcome.in_(RETRYABLE_OUTCOMES),
                )
                .group_by(JobItem.job_id, JobItem.status)
            )
            for job_id, item_status, count in retryable_rows:
                if item_status == "failed":
                    retryable_failed_by_job[int(job_id)] = int(count)
                elif item_status in {"pending", "leased"}:
                    retryable_pending_by_job[int(job_id)] = (
                        retryable_pending_by_job.get(int(job_id), 0) + int(count)
                    )
        return TEMPLATES.TemplateResponse(
            request=request,
            name="jobs.html",
            context=page_context(
                request,
                user,
                jobs=jobs,
                retryable_pending_by_job=retryable_pending_by_job,
                retryable_failed_by_job=retryable_failed_by_job,
            ),
        )

    @app.get("/admin/logs", response_class=HTMLResponse)
    def logs_page(request: Request, session: Session = Depends(get_db)):
        user = require_browser_user(request, session)
        if isinstance(user, RedirectResponse):
            return user
        selected_raw = request.query_params.get("job", "").strip()
        selected_job_id = int(selected_raw) if selected_raw.isdigit() else None
        event_query = select(JobEvent)
        if selected_job_id is not None:
            event_query = event_query.where(JobEvent.job_id == selected_job_id)
        events = list(
            session.scalars(
                event_query.order_by(JobEvent.created_at.desc()).limit(
                    1_000 if selected_job_id is not None else 300
                )
            )
        )
        jobs = {job.id: job for job in session.scalars(select(Job).where(Job.id.in_([event.job_id for event in events])))}
        return TEMPLATES.TemplateResponse(request=request, name="logs.html", context=page_context(request, user, events=events, jobs=jobs, selected_job_id=selected_job_id))

    @app.get("/admin/notifications", response_class=HTMLResponse)
    def notifications_page(request: Request, session: Session = Depends(get_db)):
        user = require_browser_user(request, session)
        if isinstance(user, RedirectResponse):
            return user
        notifications = list(
            session.scalars(
                select(NotificationOutbox)
                .order_by(NotificationOutbox.created_at.desc())
                .limit(300)
            )
        )
        job_ids = {
            notification.job_id
            for notification in notifications
            if notification.job_id is not None
        }
        jobs = (
            {
                job.id: job
                for job in session.scalars(select(Job).where(Job.id.in_(job_ids)))
            }
            if job_ids
            else {}
        )
        return TEMPLATES.TemplateResponse(
            request=request,
            name="notifications.html",
            context=page_context(
                request,
                user,
                notifications=notifications,
                jobs=jobs,
            ),
        )

    @app.post("/admin/notifications/{notification_id}/{action}")
    def notification_control(
        notification_id: int,
        action: str,
        request: Request,
        csrf: str = Form(...),
        session: Session = Depends(get_db),
    ):
        user = require_browser_user(request, session, write_access=True)
        if isinstance(user, RedirectResponse):
            return user
        validate_csrf(request, csrf)
        notification = session.scalar(
            select(NotificationOutbox)
            .where(NotificationOutbox.id == notification_id)
            .with_for_update()
        )
        if not notification:
            raise HTTPException(status_code=404, detail="Envio não encontrado.")
        try:
            if notification.status == "processing":
                raise ValueError(
                    "O envio está em andamento. Aguarde a conclusão ou a expiração do lease."
                )
            if action == "retry":
                if notification.status == "sent":
                    raise ValueError("Um envio concluído não será duplicado.")
                notification.status = "pending"
                notification.attempts = 0
                notification.next_attempt_at = None
                notification.locked_by = None
                notification.locked_until = None
                notification.last_error = None
                message = "Envio devolvido à fila."
            elif action == "cancel":
                if notification.status == "sent":
                    raise ValueError("Um envio concluído não pode ser cancelado.")
                notification.status = "cancelled"
                notification.next_attempt_at = None
                notification.locked_by = None
                notification.locked_until = None
                message = "Envio cancelado."
            else:
                raise ValueError("Ação de envio inválida.")
            audit(
                session,
                actor_id=user.id,
                action=f"notification.{action}",
                target_type="notification_outbox",
                target_id=str(notification.id),
                ip_address=client_ip(request),
                details={"job_id": notification.job_id},
            )
            session.commit()
            request.session["flash"] = {"level": "success", "message": message}
        except ValueError as exc:
            session.rollback()
            request.session["flash"] = {"level": "error", "message": str(exc)}
        return RedirectResponse("/admin/notifications", status_code=303)

    def control_job(session: Session, job: Job, action: str) -> str:
        if action == "pause":
            if job.status not in {"queued", "running"}:
                raise ValueError("Apenas jobs em fila ou execução podem ser pausados.")
            session.execute(
                update(JobItem)
                .where(JobItem.job_id == job.id, JobItem.status == "leased")
                .values(
                    status="pending",
                    credential_id=None,
                    lease_owner=None,
                    lease_expires_at=None,
                    next_attempt_at=None,
                    # Pausa administrativa não é falha do portal e não pode
                    # consumir o orçamento de retentativas daquele registro.
                    max_attempts=JobItem.max_attempts + 1,
                )
            )
            session.execute(
                update(JobItemAttempt)
                .where(
                    JobItemAttempt.job_item_id.in_(
                        select(JobItem.id).where(JobItem.job_id == job.id)
                    ),
                    JobItemAttempt.status == "started",
                )
                .values(
                    status="abandoned",
                    error_category="operator_pause",
                    error_message="Tentativa interrompida pela pausa do job.",
                    finished_at=datetime.now(UTC),
                )
            )
            session.execute(delete(CredentialLease).where(CredentialLease.job_id == job.id))
            job.status = "paused"
            message = "Job pausado pelo operador."
        elif action == "cancel":
            if job.status in {"completed", "cancelled"}:
                raise ValueError("Este job não pode mais ser interrompido.")
            session.execute(
                update(JobItemAttempt)
                .where(
                    JobItemAttempt.job_item_id.in_(
                        select(JobItem.id).where(JobItem.job_id == job.id)
                    ),
                    JobItemAttempt.status == "started",
                )
                .values(
                    status="abandoned",
                    error_category="operator_cancel",
                    error_message="Tentativa interrompida pelo cancelamento do job.",
                    finished_at=datetime.now(UTC),
                )
            )
            session.execute(
                update(JobItem)
                .where(JobItem.job_id == job.id, JobItem.status.in_(["pending", "leased"]))
                .values(
                    status="cancelled",
                    credential_id=None,
                    lease_owner=None,
                    lease_expires_at=None,
                    next_attempt_at=None,
                    finished_at=datetime.now(UTC),
                )
            )
            session.execute(delete(CredentialLease).where(CredentialLease.job_id == job.id))
            job.status = "cancelled"
            job.cancelled_at = datetime.now(UTC)
            job.finished_at = job.cancelled_at
            message = "Job interrompido totalmente pelo operador."
        elif action == "resume":
            if job.status not in {"paused", "blocked"}:
                raise ValueError("Somente jobs pausados ou bloqueados podem ser retomados.")
            job.status = "queued"
            job.cancelled_at = None
            job.finished_at = None
            message = "Job retomado pelo operador."
        elif action == "retry":
            if job.status not in {"failed", "completed_with_errors", "cancelled"}:
                raise ValueError(
                    "Tente novamente apenas jobs cancelados ou concluídos com falhas."
                )
            # Trava toda a outbox antes de alterar a geração. O claim usa
            # SKIP LOCKED e não consegue iniciar um envio antigo entre esta
            # verificação e o commit.
            notifications = list(
                session.scalars(
                    select(NotificationOutbox)
                    .where(NotificationOutbox.job_id == job.id)
                    .with_for_update()
                )
            )
            if any(item.status == "processing" for item in notifications):
                raise ValueError(
                    "O resultado ainda está sendo enviado. Aguarde o envio terminar antes de tentar novamente."
                )
            session.execute(
                update(JobItemAttempt)
                .where(
                    JobItemAttempt.job_item_id.in_(
                        select(JobItem.id).where(JobItem.job_id == job.id)
                    ),
                    JobItemAttempt.status == "started",
                )
                .values(
                    status="abandoned",
                    error_category="operator_retry",
                    error_message="Tentativa anterior substituída por retry manual.",
                    finished_at=datetime.now(UTC),
                )
            )
            for notification in notifications:
                if notification.status in {"pending", "retry", "failed"}:
                    notification.status = "cancelled"
                    notification.next_attempt_at = None
                    notification.locked_by = None
                    notification.locked_until = None
            retry_item_ids = select(JobItem.id).where(
                JobItem.job_id == job.id,
                JobItem.status.in_(["failed", "cancelled", "leased"]),
            )
            # Mantém o ciphertext anterior até existir uma nova resposta, mas
            # marca a geração como obsoleta. Assim um novo erro nunca exporta
            # campos da tentativa anterior.
            session.execute(
                update(ConsultationResult)
                .where(ConsultationResult.job_item_id.in_(retry_item_ids))
                .values(superseded_at=datetime.now(UTC))
            )
            session.execute(
                update(JobItem)
                .where(JobItem.job_id == job.id, JobItem.status.in_(["failed", "cancelled", "leased"]))
                .values(
                    status="pending",
                    outcome=None,
                    credential_id=None,
                    lease_owner=None,
                    lease_expires_at=None,
                    error_code=None,
                    error_message=None,
                    last_error_category=None,
                    next_attempt_at=None,
                    finished_at=None,
                    max_attempts=JobItem.attempts + 3,
                )
            )
            session.execute(delete(CredentialLease).where(CredentialLease.job_id == job.id))
            job.status = "queued"
            job.failed_items = 0
            job.retryable_items = 0
            job.permanent_items = 0
            job.cancelled_at = None
            job.finished_at = None
            message = "Job reenfileirado para nova tentativa."
        else:
            raise ValueError("Ação de job inválida.")
        session.add(JobEvent(job_id=job.id, event_type=f"job.{action}", message=message))
        return message

    @app.post("/admin/jobs/{job_id}/{action}")
    def job_control(job_id: int, action: str, request: Request, csrf: str = Form(...), session: Session = Depends(get_db)):
        user = require_browser_user(request, session, write_access=True)
        if isinstance(user, RedirectResponse):
            return user
        validate_csrf(request, csrf)
        job = session.scalar(
            select(Job).where(Job.id == job_id).with_for_update()
        )
        if not job:
            raise HTTPException(status_code=404, detail="Job não encontrado.")
        try:
            message = control_job(session, job, action)
            audit(session, actor_id=user.id, action=f"job.{action}", target_type="automation_job", target_id=str(job.id), ip_address=client_ip(request))
            session.commit()
            request.session["flash"] = {
                "level": "warning" if action in {"pause", "cancel"} else "success",
                "message": message,
            }
        except ValueError as exc:
            session.rollback()
            request.session["flash"] = {"level": "error", "message": str(exc)}
        return RedirectResponse("/admin/jobs", status_code=303)

    def build_job_export(session: Session, job_id: int) -> tuple[bytes, int]:
        return build_job_export_file(session, settings, job_id)

    @app.get("/admin/jobs/{job_id}/export.xlsx")
    def export_job(
        job_id: int,
        request: Request,
        session: Session = Depends(get_db),
    ):
        user = require_browser_user(request, session, write_access=True)
        if isinstance(user, RedirectResponse):
            return user
        job = session.get(Job, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job não encontrado.")
        payload, row_count = build_job_export(session, job_id)
        municipality = session.get(Municipality, job.municipality_slug)
        filename = job_export_filename(
            municipality.name if municipality else job.municipality_slug,
            exported_at=datetime.now(UTC),
            timezone_name=(
                municipality.timezone if municipality else "America/Fortaleza"
            ),
        )
        audit(
            session,
            actor_id=user.id,
            action="job.exported",
            target_type="automation_job",
            target_id=str(job_id),
            ip_address=client_ip(request),
            details={"rows": row_count},
        )
        session.commit()
        return StreamingResponse(
            io.BytesIO(payload),
            media_type=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-store",
            },
        )

    def job_payload(session: Session, job: Job) -> dict[str, object]:
        municipality = session.get(Municipality, job.municipality_slug)
        dataset = session.get(Dataset, job.dataset_id) if job.dataset_id else None
        execution = job_execution_state(session, job)
        return {
            "id": job.id,
            "prefeitura": job.municipality_slug,
            "nome": municipality.name if municipality else job.municipality_slug,
            "platform": municipality.platform_slug if municipality else "unknown",
            "status": job.status,
            "dataset_id": job.dataset_id,
            "dataset_name": (
                (getattr(dataset, "display_name", None) or dataset.original_filename)
                if dataset
                else None
            ),
            "created_at": job.created_at.isoformat() if job.created_at else None,
            "not_before": job.not_before.isoformat() if job.not_before else None,
            "started_at": job.started_at.isoformat() if job.started_at else None,
            "finished_at": job.finished_at.isoformat() if job.finished_at else None,
            "total_consultas": job.total_items,
            "realizadas": job.completed_items,
            "falhas": job.failed_items,
            "encontrados": getattr(job, "found_items", 0),
            "nao_encontrados": getattr(job, "not_found_items", 0),
            "erros_retentaveis": getattr(job, "retryable_items", 0),
            "erros_permanentes": getattr(job, "permanent_items", 0),
            "legado_nao_classificado": max(
                job.completed_items
                - getattr(job, "found_items", 0)
                - getattr(job, "not_found_items", 0),
                0,
            ),
            "erros_nao_classificados": max(
                job.failed_items
                - getattr(job, "retryable_items", 0)
                - getattr(job, "permanent_items", 0),
                0,
            ),
            "execution": execution,
            "executable": execution["executable"],
            "blocked_reason": None if execution["executable"] else execution["reason"],
        }

    def enqueue_reconciled_job_result(session: Session, job: Job) -> None:
        if job.status not in {"completed", "completed_with_errors", "failed"}:
            return
        notification = enqueue_job_result(session, job)
        if notification and notification.status == "pending":
            session.add(
                JobEvent(
                    job_id=job.id,
                    event_type="notification.queued",
                    message="Resultado final agendado após reconciliação de lease.",
                    event_data={
                        "notification_id": notification.id,
                        "channel": notification.channel,
                    },
                )
            )

    @app.post("/api/jobs/batch")
    def create_batch(
        payload: BatchRequest,
        _: ApiPrincipal = Depends(require_scope("jobs:write")),
        session: Session = Depends(get_db),
    ):
        if not payload.jobs:
            raise HTTPException(
                status_code=422,
                detail=(
                    "Cada job precisa informar uma base existente. Atualize o controlador "
                    "Telegram e selecione convênio + base."
                ),
            )
        requested = sorted(
            dict.fromkeys(
                (item.municipality_slug.strip().lower(), item.dataset_id)
                for item in payload.jobs
            )
        )
        created: list[dict[str, object]] = []
        skipped: list[dict[str, object]] = []
        for slug, dataset_id in requested:
            municipality = session.scalar(
                select(Municipality)
                .where(Municipality.slug == slug)
                .with_for_update()
            )
            dataset = session.get(Dataset, dataset_id)
            if (
                not municipality
                or not dataset
                or dataset.status != "ready"
                or dataset.municipality_slug != slug
            ):
                skipped.append(
                    {
                        "municipality_slug": slug,
                        "dataset_id": dataset_id,
                        "reason": "Convênio ou base indisponível.",
                    }
                )
                continue
            readiness = assess_municipality(session, municipality)
            if not readiness.can_start:
                skipped.append(
                    {
                        "municipality_slug": slug,
                        "dataset_id": dataset_id,
                        "reason": readiness.summary,
                    }
                )
                continue
            existing = session.scalar(
                select(Job.id).where(
                    Job.municipality_slug == slug,
                    Job.status.in_(["queued", "running", "paused", "blocked"]),
                )
            )
            if existing:
                skipped.append(
                    {
                        "municipality_slug": slug,
                        "dataset_id": dataset_id,
                        "reason": f"O job #{existing} já está ativo.",
                    }
                )
                continue
            job = create_job_for_dataset(
                session,
                dataset=dataset,
                requested_by_id=None,
            )
            job.telegram_user_id = payload.requested_by.telegram_user_id
            job.telegram_chat_id = payload.requested_by.telegram_chat_id
            created.append(job_payload(session, job))
        session.commit()
        return {"created": created, "skipped": skipped, "message": f"{len(created)} job(s) criado(s)."}

    @app.get("/api/jobs/status")
    def api_job_status(
        _: ApiPrincipal = Depends(require_scope("jobs:read")),
        session: Session = Depends(get_db),
    ):
        reconcile_job_ids = list(
            session.scalars(
                select(JobItem.job_id)
                .join(Job, Job.id == JobItem.job_id)
                .where(
                    Job.status.in_(["queued", "running"]),
                    JobItem.attempts >= JobItem.max_attempts,
                    or_(
                        JobItem.status == "pending",
                        (
                            (JobItem.status == "leased")
                            & (JobItem.lease_expires_at <= datetime.now(UTC))
                        ),
                    ),
                )
                .distinct()
                .order_by(JobItem.job_id)
                .limit(50)
            )
        )
        for reconcile_job_id in reconcile_job_ids:
            changed = expire_exhausted_job_items(
                session, job_id=reconcile_job_id
            )
            if changed:
                reconciled_job = session.get(Job, reconcile_job_id)
                if reconciled_job:
                    enqueue_reconciled_job_result(session, reconciled_job)
            # Libera cada Job em ordem determinística; não retenha dezenas de
            # locks até o fim do polling de todos os workers.
            session.commit()

        def fetch(
            statuses: list[str], limit: int = 100, *, newest_first: bool = False
        ) -> list[dict[str, object]]:
            jobs = session.scalars(
                select(Job)
                .where(Job.status.in_(statuses))
                .order_by(Job.created_at.desc() if newest_first else Job.created_at)
                .limit(limit)
            )
            return [job_payload(session, job) for job in jobs]

        queued = fetch(["awaiting_dataset", "queued"])
        now = datetime.now(UTC)
        workers_online = session.scalar(
            select(func.count())
            .select_from(WorkerHeartbeat)
            .where(WorkerHeartbeat.expires_at > now)
        ) or 0
        configured_capacity = sum(
            min(report.active_credentials, municipality.max_workers)
            for municipality in session.scalars(
                select(Municipality).where(Municipality.operational_status == "ready")
            )
            for report in [assess_municipality(session, municipality)]
        )
        return {
            "max_concurrency": configured_capacity,
            "workers_online": int(workers_online),
            "running": fetch(["running"]),
            "queued": queued,
            "queued_count": len(queued),
            "recent": fetch(
                ["completed", "completed_with_errors", "failed", "cancelled"],
                limit=10,
                newest_first=True,
            ),
        }

    @app.get("/api/workers/capacity")
    def api_worker_capacity(
        platform: str,
        _: ApiPrincipal = Depends(require_scope("jobs:read")),
        session: Session = Depends(get_db),
    ):
        normalized = platform.strip().lower()
        if normalized not in TRANSACTIONAL_ADAPTERS:
            raise HTTPException(status_code=404, detail="Adapter indisponível.")
        agreements = list(
            session.scalars(
                select(Municipality).where(
                    Municipality.platform_slug == normalized,
                    Municipality.enabled.is_(True),
                    Municipality.operational_status.in_(
                        ["testing", "ready", "degraded"]
                    ),
                )
            )
        )
        now = datetime.now(UTC)
        allocations: list[tuple[Municipality, int, int]] = []
        for item in agreements:
            usable_credentials = int(
                session.scalar(
                    select(func.count())
                    .select_from(PortalCredential)
                    .where(
                        PortalCredential.municipality_slug == item.slug,
                        or_(
                            PortalCredential.status == "active",
                            (
                                (PortalCredential.status == "cooldown")
                                & or_(
                                    PortalCredential.cooldown_until.is_(None),
                                    PortalCredential.cooldown_until <= now,
                                )
                            ),
                        ),
                    )
                )
                or 0
            )
            allocations.append(
                (
                    item,
                    usable_credentials,
                    min(max(item.max_workers, 0), usable_credentials),
                )
            )
        desired = min(sum(allocation for _, _, allocation in allocations), 20)
        return {
            "platform": normalized,
            "desired_workers": desired,
            "source": "database",
            "agreements": [
                {
                    "slug": item.slug,
                    "state": item.operational_status,
                    "max_workers": item.max_workers,
                    "usable_credentials": usable_credentials,
                    "allocated_workers": allocation,
                }
                for item, usable_credentials, allocation in allocations
            ],
        }

    @app.get("/api/datasets/ready")
    def api_ready_datasets(
        _: ApiPrincipal = Depends(require_scope("jobs:read")),
        session: Session = Depends(get_db),
    ):
        rows = session.execute(
            select(Dataset, Municipality, Platform)
            .join(Municipality, Municipality.slug == Dataset.municipality_slug)
            .join(Platform, Platform.slug == Municipality.platform_slug)
            .where(Dataset.status == "ready")
            .order_by(Municipality.name, Dataset.created_at.desc())
        ).all()
        datasets = []
        for dataset, municipality, platform in rows:
            readiness = assess_municipality(session, municipality)
            if not readiness.can_start:
                continue
            datasets.append(
                {
                    "id": dataset.id,
                    "municipality_slug": municipality.slug,
                    "municipality_name": municipality.name,
                    "processor": platform.name,
                    "name": getattr(dataset, "display_name", None)
                    or dataset.original_filename,
                    "rows": dataset.row_count,
                    "created_at": dataset.created_at.isoformat(),
                }
            )
        return {"datasets": datasets, "count": len(datasets)}

    @app.get("/api/jobs/queue")
    def api_queue(
        _: ApiPrincipal = Depends(require_scope("jobs:read")),
        session: Session = Depends(get_db),
    ):
        jobs = list(
            session.scalars(
                select(Job)
                .where(Job.status.in_(["awaiting_dataset", "queued"]))
                .order_by(Job.created_at)
            )
        )
        return {"queued": [job_payload(session, job) for job in jobs], "count": len(jobs)}

    @app.post("/api/jobs/queue/clear")
    def clear_queue(
        _: ApiPrincipal = Depends(require_scope("jobs:write")),
        session: Session = Depends(get_db),
    ):
        jobs = list(
            session.scalars(
                select(Job)
                .where(Job.status.in_(["awaiting_dataset", "queued"]))
                .with_for_update()
            )
        )
        for job in jobs:
            control_job(session, job, "cancel")
        session.commit()
        return {"cancelled_count": len(jobs)}

    @app.post("/api/jobs/running/stop")
    def stop_jobs(
        _: ApiPrincipal = Depends(require_scope("jobs:write")),
        session: Session = Depends(get_db),
    ):
        jobs = list(
            session.scalars(
                select(Job).where(Job.status == "running").with_for_update()
            )
        )
        for job in jobs:
            control_job(session, job, "cancel")
        session.commit()
        return {"cancelled_count": len(jobs)}

    @app.get("/api/operations/readiness")
    def api_readiness(
        _: ApiPrincipal = Depends(require_scope("jobs:read")),
        session: Session = Depends(get_db),
    ):
        municipalities = session.scalars(
            select(Municipality).order_by(Municipality.name)
        )
        return {
            "agreements": [
                assess_municipality(session, municipality).as_dict()
                for municipality in municipalities
            ]
        }

    @app.post("/api/workers/status")
    def worker_status(
        payload: WorkerStatusRequest,
        _: ApiPrincipal = Depends(require_scope("workers:execute")),
        session: Session = Depends(get_db),
    ):
        platform = session.get(Platform, payload.platform_slug)
        if not platform:
            raise HTTPException(status_code=404, detail="Processadora não cadastrada.")
        if payload.municipality_slug and not session.get(
            Municipality, payload.municipality_slug
        ):
            raise HTTPException(status_code=404, detail="Convênio não cadastrado.")
        now = datetime.now(UTC)
        heartbeat = session.get(WorkerHeartbeat, payload.worker_id) or WorkerHeartbeat(
            worker_id=payload.worker_id,
            platform_slug=payload.platform_slug,
            expires_at=now + timedelta(seconds=payload.ttl_seconds),
        )
        heartbeat.platform_slug = payload.platform_slug
        heartbeat.municipality_slug = payload.municipality_slug
        heartbeat.job_id = payload.job_id
        heartbeat.credential_id = payload.credential_id
        heartbeat.health_status = payload.health_status
        heartbeat.activity_status = payload.activity_status
        heartbeat.adapter_version = payload.adapter_version
        heartbeat.hostname = payload.hostname
        heartbeat.process_id = payload.process_id
        heartbeat.last_error = payload.last_error
        heartbeat.details_json = payload.details
        heartbeat.last_seen_at = now
        heartbeat.expires_at = now + timedelta(seconds=payload.ttl_seconds)
        session.add(heartbeat)
        session.commit()
        return {"ok": True, "expires_at": heartbeat.expires_at.isoformat()}

    @app.post("/api/workers/credentials/acquire")
    def worker_acquire_credential(
        payload: AcquireCredentialRequest,
        _: ApiPrincipal = Depends(require_scope("workers:execute")),
        session: Session = Depends(get_db),
    ):
        reconciled = expire_exhausted_job_items(session, job_id=payload.job_id)
        if reconciled:
            reconciled_job = session.get(Job, payload.job_id)
            if reconciled_job:
                enqueue_reconciled_job_result(session, reconciled_job)
            session.commit()
        job = session.scalar(
            select(Job).where(Job.id == payload.job_id).with_for_update()
        )
        if not job or job.municipality_slug != payload.municipality_slug:
            raise HTTPException(status_code=404, detail="Job não encontrado.")
        if job.status not in {"queued", "running"}:
            raise HTTPException(status_code=409, detail="Job não está executável.")
        execution = job_execution_state(session, job)
        if not execution["executable"]:
            raise HTTPException(status_code=409, detail=str(execution["reason"]))
        municipality = session.scalar(
            select(Municipality)
            .where(Municipality.slug == payload.municipality_slug)
            .with_for_update()
        )
        if not municipality or not municipality.enabled:
            raise HTTPException(status_code=409, detail="Convênio indisponível.")
        now = datetime.now(UTC)
        ready_items = session.scalar(
            select(func.count())
            .select_from(JobItem)
            .where(
                JobItem.job_id == payload.job_id,
                JobItem.attempts < JobItem.max_attempts,
                or_(
                    (
                        (JobItem.status == "pending")
                        & or_(
                            JobItem.next_attempt_at.is_(None),
                            JobItem.next_attempt_at <= now,
                        )
                    ),
                    (
                        (JobItem.status == "leased")
                        & (JobItem.lease_expires_at <= now)
                    ),
                ),
            )
        ) or 0
        job_leases = session.scalar(
            select(func.count())
            .select_from(CredentialLease)
            .where(
                CredentialLease.job_id == payload.job_id,
                CredentialLease.expires_at > now,
            )
        ) or 0
        if ready_items <= job_leases:
            raise HTTPException(
                status_code=409,
                detail="Todos os itens prontos já têm um worker reservado.",
            )
        active_workers = session.scalar(
            select(func.count())
            .select_from(CredentialLease)
            .join(
                PortalCredential,
                PortalCredential.id == CredentialLease.credential_id,
            )
            .where(
                PortalCredential.municipality_slug == payload.municipality_slug,
                CredentialLease.expires_at > now,
            )
        ) or 0
        if active_workers >= municipality.max_workers:
            raise HTTPException(status_code=409, detail="Limite de workers atingido.")
        credential = acquire_credential(
            session,
            job_id=payload.job_id,
            municipality_slug=payload.municipality_slug,
            worker_id=payload.worker_id,
            lease_seconds=payload.lease_seconds,
        )
        if not credential:
            raise HTTPException(status_code=409, detail="Nenhuma credencial disponível.")
        username, password = decrypt_portal_credential(credential, settings)
        session.commit()
        return JSONResponse(
            {
                "credential_id": credential.id,
                "username": username,
                "password": password,
                "login_url": municipality.login_url if municipality else None,
                "query_url": municipality.query_url if municipality else None,
                "settings": {
                    **credential.settings_json,
                    "portal_profile": credential.portal_profile,
                    "consignataria": (
                        credential.portal_profile or credential.consignataria
                    ),
                },
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/workers/items/claim")
    def worker_claim_items(
        payload: ClaimItemsRequest,
        _: ApiPrincipal = Depends(require_scope("workers:execute")),
        session: Session = Depends(get_db),
    ):
        job = session.get(Job, payload.job_id)
        if not job or job.status not in {"queued", "running"}:
            raise HTTPException(status_code=409, detail="Job não está executável.")
        execution = job_execution_state(session, job)
        if not execution["executable"]:
            raise HTTPException(status_code=409, detail=str(execution["reason"]))
        lease = session.scalar(
            select(CredentialLease).where(
                CredentialLease.worker_id == payload.worker_id,
                CredentialLease.credential_id == payload.credential_id,
                CredentialLease.job_id == payload.job_id,
                CredentialLease.expires_at > datetime.now(UTC),
            )
        )
        if not lease:
            raise HTTPException(status_code=409, detail="Lease de credencial inválido.")
        try:
            items = claim_job_items(
                session,
                job_id=payload.job_id,
                credential_id=payload.credential_id,
                worker_id=payload.worker_id,
                batch_size=payload.batch_size,
                lease_seconds=payload.lease_seconds,
            )
        except ValueError as exc:
            session.rollback()
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        cipher = SecretCipher(settings.master_key)
        response_items = []
        for item in items:
            record = session.get(DatasetRecord, item.dataset_record_id)
            if not record:
                continue
            response_items.append(
                {
                    "item_id": item.id,
                    "cpf": cipher.decrypt(
                        record.cpf_ciphertext,
                        context=f"record:{record.encryption_context}:cpf",
                    ),
                    "registration": record.registration,
                }
            )
        if job and job.status == "queued" and response_items:
            job.status = "running"
            job.started_at = job.started_at or datetime.now(UTC)
        session.commit()
        return JSONResponse({"items": response_items}, headers={"Cache-Control": "no-store"})

    @app.post("/api/workers/heartbeat")
    def worker_heartbeat(
        payload: WorkerRequest,
        _: ApiPrincipal = Depends(require_scope("workers:execute")),
        session: Session = Depends(get_db),
    ):
        ok = heartbeat_credential(
            session, worker_id=payload.worker_id, lease_seconds=payload.lease_seconds
        )
        session.commit()
        if not ok:
            raise HTTPException(status_code=404, detail="Lease não encontrado.")
        return {"ok": True}

    @app.post("/api/workers/credentials/report")
    def worker_report_credential(
        payload: CredentialReportRequest,
        _: ApiPrincipal = Depends(require_scope("workers:execute")),
        session: Session = Depends(get_db),
    ):
        lease = session.scalar(
            select(CredentialLease).where(
                CredentialLease.worker_id == payload.worker_id,
                CredentialLease.credential_id == payload.credential_id,
            )
        )
        credential = session.get(PortalCredential, payload.credential_id)
        if not lease or not credential:
            raise HTTPException(status_code=409, detail="Lease de credencial inválido.")
        now = datetime.now(UTC)
        if payload.outcome == "success":
            credential.status = "active"
            credential.failure_count = 0
            credential.cooldown_until = None
            credential.last_error = None
            credential.last_validated_at = now
        else:
            is_portal_unavailable = payload.outcome == "portal_unavailable"
            session.add(
                JobEvent(
                    job_id=lease.job_id,
                    event_type="portal.indisponivel" if is_portal_unavailable else "credencial.erro",
                    message=(payload.error_message or "Falha reportada pelo worker.")[:500],
                    event_data={
                        "credential_id": credential.id,
                        "outcome": payload.outcome,
                    },
                )
            )
            if not is_portal_unavailable:
                credential.failure_count += 1
            credential.last_error = payload.error_message or "Falha reportada pelo worker."
            if payload.outcome == "invalid_credentials":
                credential.status = "invalid"
                credential.cooldown_until = None
            else:
                from datetime import timedelta

                credential.status = "cooldown"
                credential.cooldown_until = now + timedelta(
                    seconds=payload.cooldown_seconds
                )
            release_credential(session, worker_id=payload.worker_id)
        session.commit()
        return {"ok": True, "credential_status": credential.status}

    @app.post("/api/workers/release")
    def worker_release(
        payload: WorkerRequest,
        _: ApiPrincipal = Depends(require_scope("workers:execute")),
        session: Session = Depends(get_db),
    ):
        release_credential(session, worker_id=payload.worker_id)
        session.commit()
        return {"ok": True}

    @app.post("/api/workers/items/complete")
    def worker_complete_item(
        payload: CompleteItemRequest,
        _: ApiPrincipal = Depends(require_scope("workers:execute")),
        session: Session = Depends(get_db),
    ):
        try:
            outcome = payload.outcome or str(payload.result_data.get("outcome") or "") or (
                "found" if payload.status == "completed" else "retryable_error"
            )
            if payload.outcome is None and payload.status == "completed":
                legacy_status = str(payload.result_data.get("Status_Robo") or "").lower()
                if "não encontrado" in legacy_status or "nao encontrado" in legacy_status:
                    outcome = "not_found"
            result_ciphertext = SecretCipher(settings.master_key).encrypt(
                json.dumps(payload.result_data, ensure_ascii=False),
                context=f"result:{payload.item_id}",
            )
            item = complete_job_item(
                session,
                worker_id=payload.worker_id,
                item_id=payload.item_id,
                status=payload.status,
                result_ciphertext=result_ciphertext,
                outcome=outcome,
                error_code=payload.error_code,
                error_message=payload.error_message,
                duration_ms=payload.duration_ms,
                stage=payload.stage,
                details=payload.details,
            )
            job = session.get(Job, item.job_id)
            notification = None
            if job and job.status in {"completed", "completed_with_errors", "failed"}:
                notification = enqueue_job_result(session, job)
            if notification and notification.status == "pending":
                session.add(
                    JobEvent(
                        job_id=job.id,
                        event_type="notification.queued",
                        message="Resultado final agendado para envio.",
                        event_data={"notification_id": notification.id, "channel": "telegram"},
                    )
                )
            session.commit()
        except ValueError as exc:
            session.rollback()
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "ok": True,
            "item_id": item.id,
            "status": item.status,
            "outcome": item.outcome,
            "next_attempt_at": (
                item.next_attempt_at.isoformat() if item.next_attempt_at else None
            ),
            "job_status": job.status if job else None,
        }

    @app.post("/api/workers/items/requeue")
    def worker_requeue_item(
        payload: RequeueItemRequest,
        _: ApiPrincipal = Depends(require_scope("workers:execute")),
        session: Session = Depends(get_db),
    ):
        try:
            item = requeue_job_item(
                session,
                worker_id=payload.worker_id,
                item_id=payload.item_id,
                reason=payload.reason,
                outcome=payload.outcome,
                error_code=payload.error_code,
                stage=payload.stage,
                retry_after_seconds=payload.retry_after_seconds,
            )
            job = session.get(Job, item.job_id)
            notification = None
            if job and job.status in {"completed", "completed_with_errors", "failed"}:
                notification = enqueue_job_result(session, job)
            if notification and notification.status == "pending":
                session.add(
                    JobEvent(
                        job_id=job.id,
                        event_type="notification.queued",
                        message="Resultado final agendado para envio.",
                        event_data={
                            "notification_id": notification.id,
                            "channel": "telegram",
                        },
                    )
                )
            session.commit()
        except ValueError as exc:
            session.rollback()
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True, "item_id": item.id, "status": item.status}

    return app

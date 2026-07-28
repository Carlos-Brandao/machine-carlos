"""FastAPI do painel e das rotas operacionais."""

from __future__ import annotations

import hmac
import io
import json
import secrets
import tempfile
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
import pandas as pd
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from machine_admin.config import Settings
from machine_admin.datasets import create_job_for_dataset, delete_dataset_blob, import_dataset
from machine_admin.db import get_db, get_session_factory, get_settings
from machine_admin.models import (
    AdminUser,
    ApiToken,
    ConsultationResult,
    CredentialLease,
    Dataset,
    DatasetRecord,
    IntegrationSecret,
    Job,
    JobEvent,
    JobItem,
    Municipality,
    PortalCredential,
)
from machine_admin.queue import (
    acquire_credential,
    claim_job_items,
    complete_job_item,
    create_waiting_job,
    heartbeat_credential,
    release_credential,
)
from machine_admin.schemas import (
    AcquireCredentialRequest,
    BatchRequest,
    ClaimItemsRequest,
    CompleteItemRequest,
    CredentialReportRequest,
    WorkerRequest,
)
from machine_admin.security import SecretCipher, hash_api_token
from machine_admin.secret_store import clear_secret_cache
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
from services.registry import enabled_municipalities
from services.telegram import TelegramNotifier


PACKAGE_DIR = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))


@dataclass(frozen=True)
class ApiPrincipal:
    name: str
    scopes: frozenset[str]
    token_id: int | None = None


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

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
        if not token or (token.expires_at and token.expires_at <= now):
            raise HTTPException(status_code=401, detail="Token inválido ou expirado.")
        token.last_used_at = now
        session.commit()
        return ApiPrincipal(token.name, frozenset(token.scopes), token.id)

    def require_scope(scope: str):
        def dependency(principal: ApiPrincipal = Depends(api_principal)) -> ApiPrincipal:
            if "*" not in principal.scopes and scope not in principal.scopes:
                raise HTTPException(status_code=403, detail="Escopo insuficiente.")
            return principal

        return dependency

    @app.get("/health")
    def health(session: Session = Depends(get_db)) -> dict[str, object]:
        session.execute(select(1))
        return {"ok": True, "database": "postgresql"}

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
        user = authenticate_admin(session, email, password)
        if not user:
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
    def logout(request: Request, csrf: str = Form(...)):
        validate_csrf(request, csrf)
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, session: Session = Depends(get_db)):
        user = require_browser_user(request, session)
        if isinstance(user, RedirectResponse):
            return user
        jobs = list(session.scalars(select(Job).order_by(Job.created_at.desc()).limit(12)))
        return TEMPLATES.TemplateResponse(
            request=request,
            name="dashboard.html",
            context=page_context(
                request,
                user,
                counts=dashboard_counts(session),
                jobs=jobs,
                robots=dashboard_robot_overview(session),
            ),
        )

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
            request.session["flash"] = "Usuário criado."
        except ValueError as exc:
            session.rollback()
            request.session["flash"] = str(exc)
        except IntegrityError:
            session.rollback()
            request.session["flash"] = "Já existe um usuário com estes dados."
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
            request.session["flash"] = str(exc)
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
        return TEMPLATES.TemplateResponse(request=request, name="credential_edit.html", context=page_context(request, user, credential=credential, municipality=session.get(Municipality, credential.municipality_slug), username=username, password=password))

    @app.post("/admin/credentials/{credential_id}/edit")
    def edit_credential(credential_id: int, request: Request, label: str = Form(...), username: str = Form(""), password: str = Form(""), consignataria: str = Form(...), csrf: str = Form(...), session: Session = Depends(get_db)):
        user = require_browser_user(request, session, admin_only=True)
        if isinstance(user, RedirectResponse):
            return user
        validate_csrf(request, csrf)
        credential = session.get(PortalCredential, credential_id)
        if not credential:
            raise HTTPException(status_code=404, detail="Credencial não encontrada.")
        try:
            update_portal_credential(session, settings, credential=credential, label=label, username=username, password=password, consignataria=consignataria)
            audit(session, actor_id=user.id, action="portal_credential.updated", target_type="portal_credential", target_id=str(credential.id), ip_address=client_ip(request))
            session.commit()
            request.session["flash"] = "Credencial atualizada."
        except ValueError as exc:
            session.rollback()
            request.session["flash"] = str(exc)
        return RedirectResponse("/admin/credentials", status_code=303)

    @app.post("/admin/credentials")
    def add_credential(
        request: Request,
        municipality_slug: str = Form(...),
        label: str = Form(...),
        username: str = Form(...),
        password: str = Form(...),
        consignataria: str = Form(...),
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
                consignataria=consignataria,
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
            request.session["flash"] = "Credencial armazenada."
        except ValueError as exc:
            session.rollback()
            request.session["flash"] = str(exc)
        except IntegrityError:
            session.rollback()
            request.session["flash"] = "Já existe uma credencial com este rótulo."
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
            credential.status = "disabled" if credential.status == "active" else "active"
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
            request.session["flash"] = "Segredo salvo; o valor não será exibido novamente."
        except ValueError as exc:
            session.rollback()
            request.session["flash"] = str(exc)
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
    async def upload_dataset(
        request: Request,
        municipality_slug: str = Form(...),
        csrf: str = Form(...),
        file: UploadFile = File(...),
        session: Session = Depends(get_db),
    ):
        user = require_browser_user(request, session, write_access=True)
        if isinstance(user, RedirectResponse):
            return user
        validate_csrf(request, csrf)
        payload = await file.read(settings.max_upload_bytes + 1)
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
            request.session["flash"] = (
                f"Base importada com {dataset.row_count} registros. "
                f"{dataset.error_message or ''}"
            ).strip()
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
            request.session["flash"] = str(exc)
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
            request.session["flash"] = "A base conflita com um registro já existente."
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
            request.session["flash"] = "Base não encontrada ou indisponível para consulta."
            return RedirectResponse("/admin/datasets", status_code=303)
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
        request.session["flash"] = f"Job #{job.id} criado a partir da base #{dataset.id}."
        return RedirectResponse("/admin/jobs", status_code=303)

    @app.get("/admin/jobs", response_class=HTMLResponse)
    def jobs_page(request: Request, session: Session = Depends(get_db)):
        user = require_browser_user(request, session)
        if isinstance(user, RedirectResponse):
            return user
        jobs = list(session.scalars(select(Job).order_by(Job.created_at.desc()).limit(200)))
        return TEMPLATES.TemplateResponse(
            request=request,
            name="jobs.html",
            context=page_context(request, user, jobs=jobs),
        )

    @app.get("/admin/logs", response_class=HTMLResponse)
    def logs_page(request: Request, session: Session = Depends(get_db)):
        user = require_browser_user(request, session)
        if isinstance(user, RedirectResponse):
            return user
        events = list(session.scalars(select(JobEvent).order_by(JobEvent.created_at.desc()).limit(300)))
        jobs = {job.id: job for job in session.scalars(select(Job).where(Job.id.in_([event.job_id for event in events])))}
        return TEMPLATES.TemplateResponse(request=request, name="logs.html", context=page_context(request, user, events=events, jobs=jobs))

    def control_job(session: Session, job: Job, action: str) -> str:
        if action == "pause":
            if job.status not in {"queued", "running"}:
                raise ValueError("Apenas jobs em fila ou execução podem ser pausados.")
            session.execute(
                update(JobItem)
                .where(JobItem.job_id == job.id, JobItem.status == "leased")
                .values(status="pending", credential_id=None, lease_owner=None, lease_expires_at=None)
            )
            session.execute(delete(CredentialLease).where(CredentialLease.job_id == job.id))
            job.status = "paused"
            message = "Job pausado pelo operador."
        elif action == "cancel":
            if job.status in {"completed", "cancelled"}:
                raise ValueError("Este job não pode mais ser interrompido.")
            session.execute(
                update(JobItem)
                .where(JobItem.job_id == job.id, JobItem.status.in_(["pending", "leased"]))
                .values(status="cancelled", lease_owner=None, lease_expires_at=None)
            )
            session.execute(delete(CredentialLease).where(CredentialLease.job_id == job.id))
            job.status = "cancelled"
            job.cancelled_at = datetime.now(UTC)
            job.finished_at = job.cancelled_at
            message = "Job interrompido totalmente pelo operador."
        elif action == "resume":
            if job.status != "paused":
                raise ValueError("Somente jobs pausados podem ser retomados.")
            job.status = "queued"
            job.cancelled_at = None
            job.finished_at = None
            message = "Job retomado pelo operador."
        elif action == "retry":
            if job.status not in {"failed", "cancelled", "paused"}:
                raise ValueError("Tente novamente apenas jobs pausados, cancelados ou com falha.")
            session.execute(
                update(JobItem)
                .where(JobItem.job_id == job.id, JobItem.status.in_(["failed", "cancelled", "leased"]))
                .values(status="pending", credential_id=None, lease_owner=None, lease_expires_at=None, error_code=None, error_message=None, finished_at=None)
            )
            session.execute(delete(CredentialLease).where(CredentialLease.job_id == job.id))
            job.status = "queued"
            job.failed_items = 0
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
        job = session.get(Job, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job não encontrado.")
        try:
            message = control_job(session, job, action)
            audit(session, actor_id=user.id, action=f"job.{action}", target_type="automation_job", target_id=str(job.id), ip_address=client_ip(request))
            session.commit()
            request.session["flash"] = message
        except ValueError as exc:
            session.rollback()
            request.session["flash"] = str(exc)
        return RedirectResponse("/admin/jobs", status_code=303)

    def build_job_export(session: Session, job_id: int) -> tuple[bytes, int]:
        rows = session.execute(
            select(JobItem, DatasetRecord, ConsultationResult)
            .join(DatasetRecord, DatasetRecord.id == JobItem.dataset_record_id)
            .outerjoin(
                ConsultationResult,
                ConsultationResult.job_item_id == JobItem.id,
            )
            .where(JobItem.job_id == job_id)
            .order_by(JobItem.id)
        ).all()
        cipher = SecretCipher(settings.master_key)
        exported: list[dict[str, object]] = []
        for item, record, result in rows:
            source = json.loads(
                cipher.decrypt(
                    record.source_ciphertext,
                    context=f"record:{record.encryption_context}:source",
                )
            )
            result_data = {}
            if result:
                result_data = json.loads(
                    cipher.decrypt(
                        result.result_ciphertext,
                        context=f"result:{item.id}",
                    )
                )
            exported.append(
                {
                    **source,
                    **result_data,
                    "Status_Item": item.status,
                    "Codigo_Erro": item.error_code,
                }
            )
        output = io.BytesIO()
        pd.DataFrame(exported).to_excel(output, index=False)
        return output.getvalue(), len(exported)

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
                "Content-Disposition": f'attachment; filename="job_{job_id}.xlsx"',
                "Cache-Control": "no-store",
            },
        )

    def job_payload(session: Session, job: Job) -> dict[str, object]:
        municipality = session.get(Municipality, job.municipality_slug)
        return {
            "id": job.id,
            "prefeitura": job.municipality_slug,
            "nome": municipality.name if municipality else job.municipality_slug,
            "platform": municipality.platform_slug if municipality else "unknown",
            "status": job.status,
            "created_at": job.created_at.isoformat() if job.created_at else None,
            "not_before": job.not_before.isoformat() if job.not_before else None,
            "started_at": job.started_at.isoformat() if job.started_at else None,
            "finished_at": job.finished_at.isoformat() if job.finished_at else None,
            "total_consultas": job.total_items,
            "realizadas": job.completed_items,
            "falhas": job.failed_items,
        }

    @app.post("/api/jobs/batch")
    def create_batch(
        payload: BatchRequest,
        _: ApiPrincipal = Depends(require_scope("jobs:write")),
        session: Session = Depends(get_db),
    ):
        allowed = {item.slug for item in enabled_municipalities()}
        requested = list(dict.fromkeys(value.strip().lower() for value in payload.prefeituras))
        invalid = [slug for slug in requested if slug not in allowed]
        if invalid:
            raise HTTPException(status_code=400, detail="Prefeituras inválidas: " + ", ".join(invalid))
        created: list[dict[str, object]] = []
        skipped: list[str] = []
        for slug in requested:
            existing = session.scalar(
                select(Job.id).where(
                    Job.municipality_slug == slug,
                    Job.status.in_(["awaiting_dataset", "queued", "running"]),
                )
            )
            if existing:
                skipped.append(slug)
                continue
            job = create_waiting_job(
                session,
                municipality_slug=slug,
                telegram_user_id=payload.requested_by.telegram_user_id,
                telegram_chat_id=payload.requested_by.telegram_chat_id,
            )
            created.append(job_payload(session, job))
        session.commit()
        return {"created": created, "skipped": skipped, "message": f"{len(created)} job(s) criado(s)."}

    @app.get("/api/jobs/status")
    def api_job_status(
        _: ApiPrincipal = Depends(require_scope("jobs:read")),
        session: Session = Depends(get_db),
    ):
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
        return {
            "max_concurrency": 3,
            "running": fetch(["running"]),
            "queued": queued,
            "queued_count": len(queued),
            "recent": fetch(
                ["completed", "failed", "cancelled"], limit=10, newest_first=True
            ),
        }

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
        now = datetime.now(UTC)
        job_ids = select(Job.id).where(
            Job.status.in_(["awaiting_dataset", "queued"])
        )
        session.execute(
            update(JobItem)
            .where(
                JobItem.job_id.in_(job_ids),
                JobItem.status.in_(["pending", "leased"]),
            )
            .values(status="cancelled", finished_at=now, lease_expires_at=None)
        )
        session.execute(delete(CredentialLease).where(CredentialLease.job_id.in_(job_ids)))
        cursor = session.execute(
            update(Job)
            .where(Job.status.in_(["awaiting_dataset", "queued"]))
            .values(status="cancelled", cancelled_at=now, finished_at=now)
        )
        session.commit()
        return {"cancelled_count": cursor.rowcount}

    @app.post("/api/jobs/running/stop")
    def stop_jobs(
        _: ApiPrincipal = Depends(require_scope("jobs:write")),
        session: Session = Depends(get_db),
    ):
        now = datetime.now(UTC)
        job_ids = select(Job.id).where(Job.status == "running")
        session.execute(
            update(JobItem)
            .where(
                JobItem.job_id.in_(job_ids),
                JobItem.status.in_(["pending", "leased"]),
            )
            .values(status="cancelled", finished_at=now, lease_expires_at=None)
        )
        session.execute(delete(CredentialLease).where(CredentialLease.job_id.in_(job_ids)))
        cursor = session.execute(
            update(Job)
            .where(Job.status == "running")
            .values(status="cancelled", cancelled_at=now, finished_at=now)
        )
        session.commit()
        return {"cancelled_count": cursor.rowcount}

    @app.post("/api/workers/credentials/acquire")
    def worker_acquire_credential(
        payload: AcquireCredentialRequest,
        _: ApiPrincipal = Depends(require_scope("workers:execute")),
        session: Session = Depends(get_db),
    ):
        job = session.get(Job, payload.job_id)
        if not job or job.municipality_slug != payload.municipality_slug:
            raise HTTPException(status_code=404, detail="Job não encontrado.")
        if job.status not in {"queued", "running"}:
            raise HTTPException(status_code=409, detail="Job não está executável.")
        municipality = session.scalar(
            select(Municipality)
            .where(Municipality.slug == payload.municipality_slug)
            .with_for_update()
        )
        if not municipality or not municipality.enabled:
            raise HTTPException(status_code=409, detail="Convênio indisponível.")
        active_workers = session.scalar(
            select(func.count())
            .select_from(CredentialLease)
            .join(
                PortalCredential,
                PortalCredential.id == CredentialLease.credential_id,
            )
            .where(
                PortalCredential.municipality_slug == payload.municipality_slug,
                CredentialLease.expires_at > datetime.now(UTC),
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
                    "consignataria": credential.consignataria,
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
        items = claim_job_items(
            session,
            job_id=payload.job_id,
            credential_id=payload.credential_id,
            worker_id=payload.worker_id,
            batch_size=payload.batch_size,
            lease_seconds=payload.lease_seconds,
        )
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
                    seconds=payload.cooldown_seconds if not is_portal_unavailable else 900
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
                error_code=payload.error_code,
                error_message=payload.error_message,
            )
            job = session.get(Job, item.job_id)
            should_notify = bool(
                job
                and job.status in {"completed", "failed"}
                and not session.scalar(
                    select(JobEvent.id).where(
                        JobEvent.job_id == job.id,
                        JobEvent.event_type.in_(
                            ["telegram.document.sent", "telegram.document.pending"]
                        ),
                    )
                )
            )
            if should_notify:
                session.add(
                    JobEvent(
                        job_id=job.id,
                        event_type="telegram.document.pending",
                        message="Arquivo final aguardando envio ao Telegram.",
                    )
                )
            session.commit()
        except ValueError as exc:
            session.rollback()
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if should_notify and job:
            workbook, _ = build_job_export(session, job.id)
            temporary_path: Path | None = None
            sent = False
            try:
                with tempfile.NamedTemporaryFile(
                    prefix=f"job_{job.id}_", suffix=".xlsx", delete=False
                ) as temporary:
                    temporary.write(workbook)
                    temporary_path = Path(temporary.name)
                notifier = TelegramNotifier.from_environment()
                if job.telegram_chat_id and notifier.client:
                    notifier = TelegramNotifier(
                        notifier.client, int(job.telegram_chat_id)
                    )
                sent = notifier.document(
                    temporary_path,
                    f"Resultado final — {job.municipality_slug} (job #{job.id})",
                )
            finally:
                if temporary_path:
                    temporary_path.unlink(missing_ok=True)
            pending_event = session.scalar(
                select(JobEvent).where(
                    JobEvent.job_id == job.id,
                    JobEvent.event_type == "telegram.document.pending",
                )
            )
            if pending_event:
                pending_event.event_type = (
                    "telegram.document.sent" if sent else "telegram.document.error"
                )
                pending_event.message = (
                    "Arquivo final enviado ao Telegram."
                    if sent
                    else "Telegram não configurado ou envio recusado."
                )
            session.commit()
        return {"ok": True, "item_id": item.id, "status": item.status}

    return app

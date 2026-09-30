"""Versioned API with existing bearer scopes AND ownership checks on every record."""
from __future__ import annotations
from typing import Literal
from fastapi import Depends, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, true
from sqlalchemy.orm import Session
from machine_admin.datasets import delete_dataset_blob, import_dataset
from machine_admin.db import get_db
from machine_admin.models import AdminUser, ApiToken, Dataset, ExportArtifact, Job, JobEvent, Municipality, NotificationOutbox, Schedule, ScheduleOccurrence, WebhookEndpoint
from machine_admin.operations import create_execution, result_page, serialize_job
from machine_admin.product_exports import media_type_for, read_export, request_export, serialize_export
from machine_admin.scheduling import create_schedule, serialize_schedule, update_schedule
from machine_admin.webhooks import create_webhook


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset_id: int = Field(gt=0)
    selected_credential_ids: list[int] | None = Field(default=None, max_length=20)
    max_parallel_accounts: int = Field(default=1, ge=1, le=20)


class ScheduleInput(Selection):
    name: str = Field(min_length=1, max_length=160)
    cron_expression: str = Field(min_length=9, max_length=120)
    timezone: str = Field(default="America/Fortaleza", max_length=64)
    enabled: bool = True
    misfire_grace_seconds: int = Field(default=300, ge=60, le=3600)


class SchedulePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=160)
    dataset_id: int | None = Field(default=None, gt=0)
    selected_credential_ids: list[int] | None = Field(default=None, max_length=20)
    max_parallel_accounts: int | None = Field(default=None, ge=1, le=20)
    cron_expression: str | None = Field(default=None, min_length=9, max_length=120)
    timezone: str | None = Field(default=None, max_length=64)
    enabled: bool | None = None
    misfire_grace_seconds: int | None = Field(default=None, ge=60, le=3600)


class ExportInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format: Literal["xlsx", "csv", "json"] = "xlsx"


class WebhookInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    url: str = Field(min_length=10, max_length=2048)


class EnabledInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


def install_product_routes(app, settings, require_scope, control_job):
    def owner_id(session, principal) -> int:
        token = session.get(ApiToken, principal.token_id)
        if not token:
            raise HTTPException(401, "Token inválido.")
        return token.owner_id

    def ownership(session, principal, column):
        user_id = owner_id(session, principal)
        user = session.get(AdminUser, user_id)
        # The route's explicit bearer scope remains mandatory for administrators.
        # Only the existing administrator role can access historical shared records.
        return true() if user and user.active and user.role == "admin" else column == user_id

    def owned(session, model, key, principal):
        owner_columns = {Job: Job.requested_by_id, Dataset: Dataset.uploaded_by_id, Schedule: Schedule.requested_by_id, WebhookEndpoint: WebhookEndpoint.owner_id}
        predicate = owner_columns[model] == owner_id(session, principal) if model is WebhookEndpoint else ownership(session, principal, owner_columns[model])
        row = session.scalar(select(model).where(model.id == key, predicate))
        if not row:
            raise HTTPException(404, "Registro não encontrado.")
        return row

    def owned_export(session, key, principal):
        artifact = session.scalar(select(ExportArtifact).join(Job, Job.id == ExportArtifact.job_id).where(ExportArtifact.id == key, ownership(session, principal, Job.requested_by_id)))
        if not artifact:
            raise HTTPException(404, "Exportação não encontrada.")
        return artifact

    @app.post("/api/v1/datasets", status_code=201)
    def upload_dataset(municipality_slug: str = Form(...), file: UploadFile = File(...), display_name: str | None = Form(None), duplicate_policy: Literal["keep_first", "reject", "keep_all"] = Form("keep_first"), principal=Depends(require_scope("datasets:write")), session: Session = Depends(get_db)):
        if not session.get(Municipality, municipality_slug):
            raise HTTPException(404, "Convênio não encontrado.")
        payload = file.file.read(settings.max_upload_bytes + 1)
        dataset = None
        try:
            dataset = import_dataset(session, settings, municipality_slug=municipality_slug, filename=file.filename or "base.xlsx", payload=payload, uploaded_by_id=owner_id(session, principal), display_name=display_name, duplicate_policy=duplicate_policy)
            session.commit()
            return {"id": dataset.id, "municipality_slug": dataset.municipality_slug, "name": dataset.display_name, "row_count": dataset.row_count, "status": dataset.status, "warnings": dataset.error_message}
        except ValueError as exc:
            session.rollback()
            if dataset:
                delete_dataset_blob(dataset.storage_path)
            raise HTTPException(422, str(exc)) from exc
        except Exception:
            session.rollback()
            if dataset:
                delete_dataset_blob(dataset.storage_path)
            raise

    @app.get("/api/v1/datasets")
    def datasets(municipality_slug: str | None = None, after_id: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500), principal=Depends(require_scope("datasets:read")), session: Session = Depends(get_db)):
        statement = select(Dataset).where(ownership(session, principal, Dataset.uploaded_by_id), Dataset.id > after_id).order_by(Dataset.id).limit(limit + 1)
        if municipality_slug:
            statement = statement.where(Dataset.municipality_slug == municipality_slug)
        rows = list(session.scalars(statement))
        return {"items": [{"id": r.id, "name": r.display_name, "municipality_slug": r.municipality_slug, "row_count": r.row_count, "status": r.status, "warnings": r.error_message} for r in rows[:limit]], "next_cursor": rows[limit - 1].id if len(rows) > limit else None}

    @app.post("/api/v1/jobs", status_code=201)
    def create_job(body: Selection, idempotency_key: str | None = Header(None, alias="Idempotency-Key", max_length=160), principal=Depends(require_scope("jobs:write")), session: Session = Depends(get_db)):
        owned(session, Dataset, body.dataset_id, principal)
        try:
            job = create_execution(session, requested_by_id=owner_id(session, principal), idempotency_key=idempotency_key, idempotency_namespace=f"api:{principal.token_id}", **body.model_dump())
            session.commit()
            return serialize_job(job)
        except ValueError as exc:
            session.rollback()
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/v1/jobs")
    def jobs(after_id: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500), principal=Depends(require_scope("jobs:read")), session: Session = Depends(get_db)):
        rows = list(session.scalars(select(Job).where(ownership(session, principal, Job.requested_by_id), Job.id > after_id).order_by(Job.id).limit(limit + 1)))
        return {"items": [serialize_job(job) for job in rows[:limit]], "next_cursor": rows[limit - 1].id if len(rows) > limit else None}

    @app.get("/api/v1/jobs/{job_id}")
    def job_detail(job_id: int, principal=Depends(require_scope("jobs:read")), session: Session = Depends(get_db)):
        return serialize_job(owned(session, Job, job_id, principal))

    @app.post("/api/v1/jobs/{job_id}/controls/{action}")
    def job_control(job_id: int, action: Literal["pause", "resume", "cancel", "retry"], principal=Depends(require_scope("jobs:write")), session: Session = Depends(get_db)):
        job = session.scalar(select(Job).where(Job.id == job_id, ownership(session, principal, Job.requested_by_id)).with_for_update())
        if not job:
            raise HTTPException(404, "Consulta não encontrada.")
        try:
            message = control_job(session, job, action)
            session.commit()
            return {**serialize_job(job), "message": message}
        except ValueError as exc:
            session.rollback()
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/v1/jobs/{job_id}/results")
    def results(job_id: int, after_id: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500), outcome: str | None = None, principal=Depends(require_scope("results:read")), session: Session = Depends(get_db)):
        owned(session, Job, job_id, principal)
        try:
            return result_page(session, settings, job_id, after_id=after_id, limit=limit, outcome=outcome)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/v1/jobs/{job_id}/events")
    def events(job_id: int, after_id: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500), principal=Depends(require_scope("jobs:read")), session: Session = Depends(get_db)):
        owned(session, Job, job_id, principal)
        rows = list(session.scalars(select(JobEvent).where(JobEvent.job_id == job_id, JobEvent.id > after_id).order_by(JobEvent.id).limit(limit + 1)))
        return {"items": [{"id": e.id, "event": e.event_type, "message": e.message, "created_at": e.created_at.isoformat()} for e in rows[:limit]], "next_cursor": rows[limit - 1].id if len(rows) > limit else None}

    @app.post("/api/v1/jobs/{job_id}/exports", status_code=202)
    def export_create(job_id: int, body: ExportInput, principal=Depends(require_scope("exports:write")), session: Session = Depends(get_db)):
        artifact = request_export(session, settings, owned(session, Job, job_id, principal), format=body.format)
        session.commit()
        return serialize_export(artifact)

    @app.get("/api/v1/jobs/{job_id}/exports")
    def exports(job_id: int, principal=Depends(require_scope("exports:read")), session: Session = Depends(get_db)):
        owned(session, Job, job_id, principal)
        return {"items": [serialize_export(row) for row in session.scalars(select(ExportArtifact).where(ExportArtifact.job_id == job_id).order_by(ExportArtifact.id.desc()).limit(100))]}

    @app.get("/api/v1/exports/{export_id}")
    def export_status(export_id: int, principal=Depends(require_scope("exports:read")), session: Session = Depends(get_db)):
        return serialize_export(owned_export(session, export_id, principal))

    @app.get("/api/v1/exports/{export_id}/download")
    def export_download(export_id: int, principal=Depends(require_scope("exports:read")), session: Session = Depends(get_db)):
        artifact = owned_export(session, export_id, principal)
        try:
            payload = read_export(settings, artifact)
        except (ValueError, OSError) as exc:
            raise HTTPException(409, "Exportação ainda não disponível ou com falha de integridade.") from exc
        return Response(payload, media_type=media_type_for(artifact.format), headers={"Content-Disposition": f'attachment; filename="{artifact.filename}"', "ETag": f'"{artifact.sha256}"', "Cache-Control": "no-store"})

    @app.get("/api/v1/schedules")
    def schedules(principal=Depends(require_scope("schedules:read")), session: Session = Depends(get_db)):
        return {"items": [serialize_schedule(row) for row in session.scalars(select(Schedule).where(ownership(session, principal, Schedule.requested_by_id)).order_by(Schedule.id).limit(500))]}

    @app.post("/api/v1/schedules", status_code=201)
    def schedule_create(body: ScheduleInput, principal=Depends(require_scope("schedules:write")), session: Session = Depends(get_db)):
        owned(session, Dataset, body.dataset_id, principal)
        try:
            schedule = create_schedule(session, requested_by_id=owner_id(session, principal), **body.model_dump())
            session.commit()
            return serialize_schedule(schedule)
        except ValueError as exc:
            session.rollback()
            raise HTTPException(422, str(exc)) from exc

    @app.patch("/api/v1/schedules/{schedule_id}")
    def schedule_update(schedule_id: int, body: SchedulePatch, principal=Depends(require_scope("schedules:write")), session: Session = Depends(get_db)):
        schedule = session.scalar(select(Schedule).where(Schedule.id == schedule_id, ownership(session, principal, Schedule.requested_by_id)).with_for_update())
        if not schedule:
            raise HTTPException(404, "Agendamento não encontrado.")
        if body.dataset_id:
            owned(session, Dataset, body.dataset_id, principal)
        try:
            update_schedule(session, schedule, **body.model_dump(exclude_unset=True, exclude_none=True))
            session.commit()
            return serialize_schedule(schedule)
        except ValueError as exc:
            session.rollback()
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/v1/schedules/{schedule_id}/occurrences")
    def occurrences(schedule_id: int, after_id: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500), principal=Depends(require_scope("schedules:read")), session: Session = Depends(get_db)):
        owned(session, Schedule, schedule_id, principal)
        rows = list(session.scalars(select(ScheduleOccurrence).where(ScheduleOccurrence.schedule_id == schedule_id, ScheduleOccurrence.id > after_id).order_by(ScheduleOccurrence.id).limit(limit + 1)))
        return {"items": [{"id": r.id, "scheduled_for": r.scheduled_for.isoformat(), "status": r.status, "job_id": r.job_id, "message": r.message} for r in rows[:limit]], "next_cursor": rows[limit - 1].id if len(rows) > limit else None}

    @app.post("/api/v1/webhooks", status_code=201)
    def webhook_create(body: WebhookInput, principal=Depends(require_scope("webhooks:write")), session: Session = Depends(get_db)):
        try:
            endpoint, secret = create_webhook(session, settings, owner_id=owner_id(session, principal), **body.model_dump())
            session.commit()
            return {"id": endpoint.id, "name": endpoint.name, "url": endpoint.url, "enabled": endpoint.enabled, "signing_secret": secret}
        except ValueError as exc:
            session.rollback()
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/v1/webhooks")
    def webhooks(principal=Depends(require_scope("webhooks:read")), session: Session = Depends(get_db)):
        return {"items": [{"id": r.id, "name": r.name, "url": r.url, "enabled": r.enabled} for r in session.scalars(select(WebhookEndpoint).where(WebhookEndpoint.owner_id == owner_id(session, principal)).order_by(WebhookEndpoint.id))]}

    @app.patch("/api/v1/webhooks/{webhook_id}")
    def webhook_update(webhook_id: int, body: EnabledInput, principal=Depends(require_scope("webhooks:write")), session: Session = Depends(get_db)):
        endpoint = owned(session, WebhookEndpoint, webhook_id, principal)
        endpoint.enabled = body.enabled
        session.commit()
        return {"id": endpoint.id, "enabled": endpoint.enabled}

    @app.get("/api/v1/webhooks/{webhook_id}/deliveries")
    def deliveries(webhook_id: int, principal=Depends(require_scope("webhooks:read")), session: Session = Depends(get_db)):
        owned(session, WebhookEndpoint, webhook_id, principal)
        return {"items": [{"id": r.id, "status": r.status, "attempts": r.attempts, "next_attempt_at": r.next_attempt_at.isoformat() if r.next_attempt_at else None, "last_error": r.last_error} for r in session.scalars(select(NotificationOutbox).where(NotificationOutbox.channel == "webhook", NotificationOutbox.recipient == str(webhook_id)).order_by(NotificationOutbox.id.desc()).limit(100))]}

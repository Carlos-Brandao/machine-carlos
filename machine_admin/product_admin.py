"""Cookie-authenticated operator screens for consultations and schedules."""

from datetime import UTC, datetime
from pathlib import Path
import secrets

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from machine_admin.db import get_db
from machine_admin.models import CredentialLease, Dataset, ExportArtifact, Job, JobEvent, JobItem, Municipality, PortalCredential


TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def install_product_admin(app, settings, require_browser_user, page_context, validate_csrf, control_job, job_execution_state):
    def render(request, user, name, **values):
        return TEMPLATES.TemplateResponse(request=request, name=name, context=page_context(request, user, **values))

    def flash(request, message, *, error=False):
        request.session["flash"] = {"message": str(message), "level": "error" if error else "success"}

    def catalog(session):
        datasets = list(session.scalars(select(Dataset).where(Dataset.status == "ready").order_by(Dataset.created_at.desc())))
        credentials = list(session.scalars(select(PortalCredential).order_by(PortalCredential.municipality_slug, PortalCredential.label)))
        municipalities = {item.slug: item for item in session.scalars(select(Municipality))}
        return {"datasets": datasets, "credentials": credentials, "municipality_map": municipalities}

    def get_job(session, job_id, *, lock=False):
        query = select(Job).where(Job.id == job_id)
        if lock:
            query = query.with_for_update()
        job = session.scalar(query)
        if not job:
            raise HTTPException(404, "Consulta não encontrada.")
        return job

    def snapshot(session, job):
        from machine_admin.operations import serialize_job
        from machine_admin.product_exports import serialize_export

        now = datetime.now(UTC)
        selected = getattr(job, "selected_credential_ids", None) or []
        query = select(PortalCredential).where(PortalCredential.municipality_slug == job.municipality_slug)
        if selected:
            query = query.where(PortalCredential.id.in_(selected))
        credentials = list(session.scalars(query.order_by(PortalCredential.label)))
        leases = {lease.credential_id: lease for lease in session.scalars(select(CredentialLease).where(CredentialLease.credential_id.in_([item.id for item in credentials]), CredentialLease.expires_at > now))}
        counts = {(credential_id, status): int(count) for credential_id, status, count in session.execute(select(JobItem.credential_id, JobItem.status, func.count(JobItem.id)).where(JobItem.job_id == job.id).group_by(JobItem.credential_id, JobItem.status))}
        accounts = []
        for credential in credentials:
            lease = leases.get(credential.id)
            usable = credential.status == "active" or (credential.status == "cooldown" and (credential.cooldown_until is None or credential.cooldown_until <= now))
            accounts.append({"id": credential.id, "label": credential.label, "status": credential.status, "available": usable and lease is None, "in_use": bool(lease), "current_job_id": lease.job_id if lease else None, "completed": counts.get((credential.id, "completed"), 0), "in_progress": counts.get((credential.id, "leased"), 0), "last_error": credential.last_error, "cooldown_until": credential.cooldown_until.isoformat() if credential.cooldown_until else None})
        events = list(session.scalars(select(JobEvent).where(JobEvent.job_id == job.id).order_by(JobEvent.id.desc()).limit(25)))
        dataset = session.get(Dataset, job.dataset_id) if job.dataset_id else None
        municipality = session.get(Municipality, job.municipality_slug)
        usable_accounts = sum(1 for item in accounts if item["available"] or (item["in_use"] and item["current_job_id"] == job.id))
        effective_limit = min(getattr(job, "max_parallel_accounts", 1) or 1, municipality.max_workers if municipality else 0, usable_accounts)
        payload = serialize_job(job)
        payload.update({"id": job.id, "status": job.status, "municipality": municipality.name if municipality else job.municipality_slug, "dataset_name": (dataset.display_name or dataset.original_filename) if dataset else None, "total": job.total_items, "completed": job.completed_items, "failed": job.failed_items, "found": job.found_items, "not_found": job.not_found_items, "pending": max(0, job.total_items-job.completed_items-job.failed_items), "max_parallel_accounts": getattr(job, "max_parallel_accounts", 1), "accounts": accounts, "execution": job_execution_state(session, job), "events": [{"id": event.id, "type": event.event_type, "message": event.message, "at": event.created_at.isoformat()} for event in events], "updated_at": now.isoformat()})
        online_workers = payload["execution"].get("readiness", {}).get("online_workers")
        if online_workers is not None:
            effective_limit = min(effective_limit, int(online_workers))
        payload["capacity"] = {"effective_limit": effective_limit, "agreement_limit": municipality.max_workers if municipality else 0, "selected_accounts": len(accounts), "available_for_job": usable_accounts}
        payload["exports"] = []
        for artifact in session.scalars(select(ExportArtifact).where(ExportArtifact.job_id == job.id).order_by(ExportArtifact.id.desc()).limit(20)):
            entry = serialize_export(artifact)
            entry["download_url"] = f"/admin/exports/{artifact.id}/download" if artifact.status == "ready" else None
            payload["exports"].append(entry)
        return payload

    @app.get("/admin/consultations/new", response_class=HTMLResponse)
    def new_consultation(request: Request, dataset_id: int | None = None, session: Session = Depends(get_db)):
        user = require_browser_user(request, session, write_access=True)
        if isinstance(user, RedirectResponse):
            return user
        return render(request, user, "consultation_new.html", selected_dataset_id=dataset_id, submission_key=secrets.token_urlsafe(24), **catalog(session))

    @app.post("/admin/consultations/new")
    def create_consultation(request: Request, dataset_id: int = Form(...), credential_ids: list[int] = Form(...), max_parallel_accounts: int = Form(1), submission_key: str = Form(...), csrf: str = Form(...), session: Session = Depends(get_db)):
        from machine_admin.operations import create_execution

        user = require_browser_user(request, session, write_access=True)
        if isinstance(user, RedirectResponse):
            return user
        validate_csrf(request, csrf)
        try:
            job = create_execution(session, dataset_id=dataset_id, requested_by_id=user.id, selected_credential_ids=credential_ids, max_parallel_accounts=max_parallel_accounts, idempotency_key=submission_key)
            session.commit()
        except ValueError as exc:
            session.rollback()
            flash(request, exc, error=True)
            return RedirectResponse(f"/admin/consultations/new?dataset_id={dataset_id}", 303)
        flash(request, f"Consulta #{job.id} criada. Acompanhe o progresso e os acessos abaixo.")
        return RedirectResponse(f"/admin/consultations/{job.id}", 303)

    @app.get("/admin/consultations/{job_id}", response_class=HTMLResponse)
    def consultation_detail(job_id: int, request: Request, session: Session = Depends(get_db)):
        user = require_browser_user(request, session)
        if isinstance(user, RedirectResponse):
            return user
        job = get_job(session, job_id)
        return render(request, user, "consultation_detail.html", job=job, summary=snapshot(session, job))

    @app.get("/admin/consultations/{job_id}/status")
    def consultation_status(job_id: int, request: Request, session: Session = Depends(get_db)):
        user = require_browser_user(request, session)
        if isinstance(user, RedirectResponse):
            return JSONResponse({"detail": "Sua sessão expirou. Entre novamente."}, status_code=401)
        return JSONResponse(snapshot(session, get_job(session, job_id)), headers={"Cache-Control": "no-store"})

    @app.get("/admin/consultations/{job_id}/results")
    def consultation_results(job_id: int, request: Request, after_id: int = 0, limit: int = 25, outcome: str | None = None, session: Session = Depends(get_db)):
        from machine_admin.operations import result_page

        user = require_browser_user(request, session)
        if isinstance(user, RedirectResponse):
            return JSONResponse({"detail": "Sua sessão expirou. Entre novamente."}, status_code=401)
        get_job(session, job_id)
        try:
            data = result_page(session, settings, job_id, after_id=max(0, after_id), limit=max(1, min(limit, 100)), outcome=outcome)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return JSONResponse(data, headers={"Cache-Control": "no-store"})

    # Register this concrete POST before the generic job-control route below.
    @app.post("/admin/consultations/{job_id}/exports")
    def consultation_export(job_id: int, request: Request, format: str = Form("xlsx"), csrf: str = Form(...), session: Session = Depends(get_db)):
        from machine_admin.product_exports import request_export

        user = require_browser_user(request, session, write_access=True)
        if isinstance(user, RedirectResponse):
            return user
        validate_csrf(request, csrf)
        try:
            artifact = request_export(session, settings, get_job(session, job_id), format=format)
            session.commit()
            flash(request, "Arquivo pronto para baixar." if artifact.status == "ready" else "Exportação solicitada. O estado será atualizado automaticamente abaixo.")
        except ValueError as exc:
            session.rollback()
            flash(request, exc, error=True)
        return RedirectResponse(f"/admin/consultations/{job_id}#exports", 303)

    @app.get("/admin/exports/{export_id}/download")
    def download_export(export_id: int, request: Request, session: Session = Depends(get_db)):
        from machine_admin.product_exports import media_type_for, read_export

        user = require_browser_user(request, session, write_access=True)
        if isinstance(user, RedirectResponse):
            return user
        artifact = session.get(ExportArtifact, export_id)
        if not artifact:
            raise HTTPException(404, "Exportação não encontrada.")
        try:
            content = read_export(settings, artifact)
        except (ValueError, OSError) as exc:
            raise HTTPException(409, "Arquivo ainda não está pronto ou falhou na verificação de integridade.") from exc
        return Response(content, media_type=media_type_for(artifact.format), headers={"Content-Disposition": f'attachment; filename="{artifact.filename}"', "ETag": f'"{artifact.sha256}"', "Cache-Control": "no-store"})

    @app.post("/admin/consultations/{job_id}/{action}")
    def consultation_action(job_id: int, action: str, request: Request, csrf: str = Form(...), session: Session = Depends(get_db)):
        user = require_browser_user(request, session, write_access=True)
        if isinstance(user, RedirectResponse):
            return user
        validate_csrf(request, csrf)
        try:
            message = control_job(session, get_job(session, job_id, lock=True), action)
            session.commit()
            flash(request, message)
        except ValueError as exc:
            session.rollback()
            flash(request, exc, error=True)
        return RedirectResponse(f"/admin/consultations/{job_id}", 303)

    @app.get("/admin/schedules", response_class=HTMLResponse)
    def schedules_page(request: Request, session: Session = Depends(get_db)):
        from machine_admin.models import Schedule, ScheduleOccurrence
        from machine_admin.scheduling import serialize_schedule

        user = require_browser_user(request, session)
        if isinstance(user, RedirectResponse):
            return user
        schedules = [serialize_schedule(item) for item in session.scalars(select(Schedule).order_by(Schedule.id.desc()))]
        occurrences = list(session.scalars(select(ScheduleOccurrence).order_by(ScheduleOccurrence.id.desc()).limit(50)))
        values = catalog(session)
        datasets = {item.id: item for item in values["datasets"]}
        labels = {item.id: item.label for item in values["credentials"]}
        for schedule in schedules:
            dataset = datasets.get(schedule["dataset_id"])
            schedule["dataset_name"] = (dataset.display_name or dataset.original_filename) if dataset else f"Base #{schedule['dataset_id']}"
            schedule["account_labels"] = [labels.get(identity, f"Acesso #{identity}") for identity in schedule["selected_credential_ids"] or []]
        return render(request, user, "schedules.html", schedules=schedules, occurrences=occurrences, **values)

    @app.post("/admin/schedules")
    def add_schedule(request: Request, name: str = Form(...), dataset_id: int = Form(...), credential_ids: list[int] = Form(...), max_parallel_accounts: int = Form(1), cron_expression: str = Form(...), timezone: str = Form("America/Fortaleza"), csrf: str = Form(...), session: Session = Depends(get_db)):
        from machine_admin.scheduling import create_schedule

        user = require_browser_user(request, session, write_access=True)
        if isinstance(user, RedirectResponse):
            return user
        validate_csrf(request, csrf)
        try:
            create_schedule(session, name=name, dataset_id=dataset_id, requested_by_id=user.id, cron_expression=cron_expression, timezone=timezone, selected_credential_ids=credential_ids, max_parallel_accounts=max_parallel_accounts)
            session.commit()
            flash(request, "Agendamento criado. Confira as próximas execuções abaixo.")
        except ValueError as exc:
            session.rollback()
            flash(request, exc, error=True)
        return RedirectResponse("/admin/schedules", 303)

    @app.post("/admin/schedules/{schedule_id}/toggle")
    def toggle_schedule(schedule_id: int, request: Request, csrf: str = Form(...), session: Session = Depends(get_db)):
        from machine_admin.models import Schedule
        from machine_admin.scheduling import update_schedule

        user = require_browser_user(request, session, write_access=True)
        if isinstance(user, RedirectResponse):
            return user
        validate_csrf(request, csrf)
        schedule = session.scalar(select(Schedule).where(Schedule.id == schedule_id).with_for_update())
        if not schedule:
            raise HTTPException(404, "Agendamento não encontrado.")
        try:
            update_schedule(session, schedule, enabled=not schedule.enabled)
            session.commit()
            flash(request, "Agendamento ativado." if schedule.enabled else "Agendamento pausado. Consultas já iniciadas seguem seus próprios controles.")
        except ValueError as exc:
            session.rollback()
            flash(request, exc, error=True)
        return RedirectResponse("/admin/schedules", 303)

    @app.get("/admin/settings", response_class=HTMLResponse)
    def settings_page(request: Request, session: Session = Depends(get_db)):
        user = require_browser_user(request, session)
        if isinstance(user, RedirectResponse):
            return user
        return render(request, user, "settings.html")

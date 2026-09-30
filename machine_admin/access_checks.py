"""Testes de acesso explícitos, reservados como qualquer sessão de consulta."""
from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, or_, select

from machine_admin.db import get_db
from machine_admin.models import AccessCheck, CredentialLease, Municipality, PortalCredential
from machine_admin.queue import apply_credential_report
from machine_admin.services import audit, decrypt_portal_credential


class CheckClaim(BaseModel):
    worker_id: str = Field(min_length=3, max_length=160)
    platform_slug: str = Field(min_length=1, max_length=64)
    lease_seconds: int = Field(default=120, ge=30, le=600)


class CheckComplete(BaseModel):
    worker_id: str = Field(min_length=3, max_length=160)
    credential_lease_token: str = Field(min_length=32, max_length=64)
    outcome: Literal['success', 'transient_failure', 'invalid_credentials',
                     'portal_unavailable', 'integration_unavailable']
    error_code: str | None = Field(default=None, max_length=80)
    message: str | None = Field(default=None, max_length=1000)


def expire_access_checks(session):
    now = datetime.now(UTC)
    for check in session.scalars(select(AccessCheck).where(
        AccessCheck.status == 'running').with_for_update(skip_locked=True)):
        lease = session.get(CredentialLease, check.credential_id)
        if not lease or lease.lease_token != check.lease_token or lease.expires_at <= now:
            check.status = 'failed'
            check.error_code = 'test_interrupted'
            check.message = 'Executor desconectado. Nenhuma consulta à base foi iniciada.'
            check.finished_at = now


def install_access_checks(app, settings, require_scope, require_browser_user, validate_csrf):
    @app.post('/admin/credentials/{credential_id}/test')
    def request_check(credential_id: int, request: Request, csrf: str = Form(...),
                      session=Depends(get_db)):
        user = require_browser_user(request, session, admin_only=True)
        if isinstance(user, RedirectResponse):
            return user
        validate_csrf(request, csrf)
        credential = session.scalar(select(PortalCredential).where(
            PortalCredential.id == credential_id).with_for_update())
        if not credential:
            raise HTTPException(404, 'Acesso não encontrado.')
        latest = session.scalar(select(AccessCheck).where(
            AccessCheck.credential_id == credential_id).order_by(AccessCheck.id.desc()).limit(1))
        now = datetime.now(UTC)
        if credential.status == 'disabled' or not credential.login_identity:
            message = 'Ative um acesso único antes de testar.'
        elif latest and latest.status in {'queued', 'running'}:
            message = 'Já há um teste aguardando ou em execução para este acesso.'
        elif latest and latest.created_at > now - timedelta(minutes=15):
            message = 'Aguarde 15 minutos entre testes para evitar consumo repetido de captcha.'
        else:
            session.add(AccessCheck(credential_id=credential_id, requested_by_id=user.id,
                status='queued', message='Aguardando executor e liberação da conta.'))
            audit(session, actor_id=user.id, action='access.test_requested',
                  target_type='portal_credential', target_id=str(credential_id))
            message = 'Teste agendado. Ele verifica apenas o login e pode consumir captcha.'
        session.commit()
        request.session['flash'] = {'level': 'info', 'message': message}
        return RedirectResponse(f'/admin/credentials/{credential_id}/edit', status_code=303)

    @app.post('/api/workers/access-checks/claim')
    def claim_check(payload: CheckClaim, principal=Depends(require_scope('workers:execute')),
                    session=Depends(get_db)):
        now = datetime.now(UTC)
        expire_access_checks(session)
        # Lock da credencial é comum à fila normal: não abre dois navegadores
        # com o mesmo login, mesmo quando o teste e a consulta chegam juntos.
        checks = session.scalars(select(AccessCheck).join(PortalCredential).join(
            Municipality, Municipality.slug == PortalCredential.municipality_slug).where(
                AccessCheck.status == 'queued', Municipality.platform_slug == payload.platform_slug
            ).order_by(AccessCheck.id).with_for_update(skip_locked=True, of=AccessCheck).limit(20))
        for check in checks:
            credential = session.scalar(select(PortalCredential).where(
                PortalCredential.id == check.credential_id).with_for_update(skip_locked=True))
            if not credential:
                continue
            if credential.status == 'disabled' or not credential.login_identity:
                check.status, check.message, check.finished_at = 'failed', 'Acesso desativado ou duplicado.', now
                continue
            lease = session.get(CredentialLease, credential.id)
            if lease and lease.expires_at > now:
                continue
            if session.scalar(select(CredentialLease.credential_id).where(
                CredentialLease.worker_id == payload.worker_id, CredentialLease.expires_at > now)):
                break
            session.execute(delete(CredentialLease).where(
                CredentialLease.expires_at <= now,
                or_(CredentialLease.credential_id == credential.id,
                    CredentialLease.worker_id == payload.worker_id)))
            token = secrets.token_hex(24)
            until = now + timedelta(seconds=payload.lease_seconds)
            session.add(CredentialLease(credential_id=credential.id, job_id=None,
                worker_id=payload.worker_id, lease_token=token, heartbeat_at=now, expires_at=until))
            check.status, check.worker_id, check.lease_token = 'running', payload.worker_id, token
            check.started_at, check.expires_at = now, until
            municipality = session.get(Municipality, credential.municipality_slug)
            username, password = decrypt_portal_credential(credential, settings)
            response = {'check_id': check.id, 'municipality_slug': municipality.slug,
                'credential': {'credential_id': credential.id, 'lease_token': token,
                    'username': username, 'password': password,
                    'login_url': municipality.login_url, 'query_url': municipality.query_url,
                    'settings': {**credential.settings_json, 'portal_profile': credential.portal_profile,
                        'consignataria': credential.portal_profile or credential.consignataria}}}
            session.commit()
            return JSONResponse(response, headers={'Cache-Control': 'no-store'})
        session.commit()
        return {'check_id': None}

    @app.post('/api/workers/access-checks/{check_id}/complete')
    def complete_check(check_id: int, payload: CheckComplete,
                       principal=Depends(require_scope('workers:execute')), session=Depends(get_db)):
        check = session.scalar(select(AccessCheck).where(AccessCheck.id == check_id).with_for_update())
        if not check or check.worker_id != payload.worker_id or check.lease_token != payload.credential_lease_token:
            raise HTTPException(409, 'Teste não pertence a este executor.')
        if check.status in {'success', 'failed'}:
            return {'ok': True, 'status': check.status}
        lease = session.get(CredentialLease, check.credential_id)
        if not lease or lease.lease_token != payload.credential_lease_token or lease.expires_at <= datetime.now(UTC):
            raise HTTPException(409, 'Reserva do teste expirou.')
        credential = session.get(PortalCredential, check.credential_id)
        # Redigir segredos mesmo quando a mensagem foi produzida pelo driver.
        message = payload.message or ('Login confirmado.' if payload.outcome == 'success' else 'Login não confirmado.')
        for secret in decrypt_portal_credential(credential, settings):
            if secret:
                message = message.replace(secret, '[oculto]')
        apply_credential_report(session, credential, outcome=payload.outcome, stage='login',
            error_code=payload.error_code, error_message=message)
        check.status = 'success' if payload.outcome == 'success' else 'failed'
        check.error_code, check.message, check.finished_at = payload.error_code, message, datetime.now(UTC)
        session.commit()
        return {'ok': True, 'status': check.status}

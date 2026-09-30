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


ACCESS_CHECK_TIMEOUT_SECONDS = 300
ACTIVE_CHECK_STATES = {'queued', 'running', 'cancelling'}


def _live_check_lease(session, check, now=None):
    lease = session.get(CredentialLease, check.credential_id)
    moment = now or datetime.now(UTC)
    return lease if (lease and lease.lease_token == check.lease_token
                     and lease.expires_at > moment) else None


def _finish_interrupted_check(check, now):
    if check.status == 'running' and check.expires_at and check.expires_at <= now:
        check.error_code = 'test_timeout'
    if check.status == 'cancelling' and check.error_code != 'test_timeout':
        check.status = 'cancelled'
        check.message = 'Teste cancelado; a sessão foi encerrada ou sua reserva expirou.'
    else:
        check.status = 'failed'
        check.error_code = check.error_code or 'test_interrupted'
        check.message = ('Tempo limite do teste atingido. Nenhuma consulta à base foi iniciada.'
                         if check.error_code == 'test_timeout' else
                         'Executor desconectado. Nenhuma consulta à base foi iniciada.')
    check.finished_at = now


def access_check_should_drain(session, lease):
    """Used by heartbeat for a login test, which deliberately has no Job."""
    check = session.scalar(select(AccessCheck).where(
        AccessCheck.credential_id == lease.credential_id,
        AccessCheck.lease_token == lease.lease_token,
        AccessCheck.worker_id == lease.worker_id).with_for_update())
    if not check:
        return True
    if check.status == 'running' and check.expires_at and check.expires_at <= datetime.now(UTC):
        check.status = 'cancelling'
        check.error_code = 'test_timeout'
        check.message = 'Tempo limite atingido; encerrando a sessão do teste.'
    return check.status != 'running'


def finalize_released_access_check(session, *, worker_id, lease_token):
    check = session.scalar(select(AccessCheck).where(
        AccessCheck.worker_id == worker_id, AccessCheck.lease_token == lease_token
    ).with_for_update())
    if check and check.status in {'running', 'cancelling'}:
        _finish_interrupted_check(check, datetime.now(UTC))


def cancel_access_check(session, check):
    """Cancels queued work immediately, but never releases a live browser's login."""
    now = datetime.now(UTC)
    if check.status not in ACTIVE_CHECK_STATES:
        return check.status
    live = _live_check_lease(session, check, now)
    if live:
        check.status = 'cancelling'
        check.message = 'Cancelamento solicitado; aguardando o encerramento seguro da sessão.'
        check.finished_at = None
    elif check.status in ACTIVE_CHECK_STATES:
        check.status = 'cancelled'
        check.message = 'Teste cancelado antes de iniciar ou após encerramento da sessão.'
        check.finished_at = now
    return check.status


def _test_policy(credential, check, session):
    now = datetime.now(UTC)
    if credential.status == 'disabled' or not credential.login_identity:
        return False, 0, 'Ative um acesso único antes de testar.'
    if check and (check.status in ACTIVE_CHECK_STATES or _live_check_lease(session, check, now)):
        return False, 0, 'Já há um teste aguardando, em execução ou encerrando a sessão.'
    if check and check.created_at and not (check.status == 'cancelled' and check.started_at is None):
        remaining = max(0, int((check.created_at + timedelta(minutes=15) - now).total_seconds()))
        if remaining:
            return False, remaining, 'Aguarde entre testes para evitar consumo repetido de captcha.'
    return True, 0, None


def _serialize_check(session, check):
    live = bool(_live_check_lease(session, check))
    message, blocking_job_id = check.message, None
    if check.status == 'queued':
        assignment = session.get(CredentialLease, check.credential_id)
        if assignment and assignment.expires_at > datetime.now(UTC):
            blocking_job_id = assignment.job_id
            message = (f'Aguardando a consulta #{blocking_job_id} liberar este acesso. '
                       'Para testar agora, pause a consulta e aguarde encerrar a sessão.'
                       if blocking_job_id else 'Aguardando outro teste encerrar a sessão deste acesso.')
    return {'id': check.id, 'status': check.status, 'message': message,
            'blocking_job_id': blocking_job_id,
            'error_code': check.error_code, 'can_cancel': check.status in {'queued', 'running'},
            'session_closing': live and check.status in {'success', 'failed', 'cancelling'},
            **{key: getattr(check, key).isoformat() if getattr(check, key) else None
               for key in ('created_at', 'started_at', 'finished_at')}}


def expire_access_checks(session):
    now = datetime.now(UTC)
    for check in session.scalars(select(AccessCheck).where(
        AccessCheck.status.in_({'running', 'cancelling'})).with_for_update(skip_locked=True)):
        if not _live_check_lease(session, check, now):
            _finish_interrupted_check(check, now)
        elif check.status == 'running' and check.expires_at and check.expires_at <= now:
            check.status, check.error_code = 'cancelling', 'test_timeout'
            check.message = 'Tempo limite atingido; encerrando a sessão do teste.'


def install_access_checks(app, settings, require_scope, require_browser_user, validate_csrf):
    @app.get('/admin/credentials/{credential_id}/test-status')
    def check_status(credential_id: int, request: Request, session=Depends(get_db)):
        user = require_browser_user(request, session, admin_only=True)
        if isinstance(user, RedirectResponse):
            return JSONResponse({'detail': 'Sessão expirada. Entre novamente.'}, status_code=401,
                                headers={'Cache-Control': 'no-store'})
        credential = session.get(PortalCredential, credential_id)
        if not credential:
            raise HTTPException(404, 'Acesso não encontrado.')
        checks = list(session.scalars(select(AccessCheck).where(
            AccessCheck.credential_id == credential_id).order_by(AccessCheck.id.desc()).limit(10)))
        can_test, retry_after, reason = _test_policy(credential, checks[0] if checks else None, session)
        values = [_serialize_check(session, check) for check in checks]
        return JSONResponse({'latest': values[0] if values else None, 'checks': values,
            'can_test': can_test, 'retry_after_seconds': retry_after, 'reason': reason},
            headers={'Cache-Control': 'no-store'})

    @app.post('/admin/credentials/{credential_id}/tests/{check_id}/cancel')
    def cancel_check(credential_id: int, check_id: int, request: Request,
                     csrf: str = Form(...), session=Depends(get_db)):
        user = require_browser_user(request, session, admin_only=True)
        if isinstance(user, RedirectResponse):
            return user
        validate_csrf(request, csrf)
        check = session.scalar(select(AccessCheck).where(
            AccessCheck.id == check_id, AccessCheck.credential_id == credential_id).with_for_update())
        if not check:
            raise HTTPException(404, 'Teste não encontrado.')
        state = cancel_access_check(session, check)
        audit(session, actor_id=user.id, action='access.test_cancelled',
              target_type='portal_access_check', target_id=str(check.id))
        session.commit()
        request.session['flash'] = {'level': 'info', 'message':
            'Cancelamento solicitado; a conta permanece reservada até a sessão fechar.'
            if state == 'cancelling' else 'O teste não está mais em execução.'}
        return RedirectResponse(f'/admin/credentials/{credential_id}/edit', status_code=303)

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
        can_test, retry_after, message = _test_policy(credential, latest, session)
        if can_test:
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
            check.started_at = now
            check.expires_at = now + timedelta(seconds=ACCESS_CHECK_TIMEOUT_SECONDS)
            municipality = session.get(Municipality, credential.municipality_slug)
            username, password = decrypt_portal_credential(credential, settings)
            response = {'check_id': check.id, 'municipality_slug': municipality.slug,
                'timeout_seconds': ACCESS_CHECK_TIMEOUT_SECONDS,
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
        if check.status in {'success', 'failed', 'cancelled'}:
            return {'ok': True, 'status': check.status}
        if check.status == 'cancelling':
            # Cancel wins the race with a late login response. In particular it
            # must never invalidate credentials based on an aborted browser.
            return {'ok': True, 'status': check.status}
        if check.expires_at and check.expires_at <= datetime.now(UTC):
            check.status, check.error_code = 'cancelling', 'test_timeout'
            check.message = 'Tempo limite atingido; encerrando a sessão do teste.'
            session.commit()
            return {'ok': True, 'status': 'cancelling'}
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

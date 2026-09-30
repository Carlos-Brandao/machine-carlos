"""Signed, retryable webhook deliveries; exports remain available independently."""
from __future__ import annotations

import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import secrets
import socket
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from sqlalchemy import and_, func, or_, select, update

from machine_admin.models import ExportArtifact, Job, NotificationOutbox, WebhookEndpoint
from machine_admin.security import SecretCipher


def validate_webhook_url(url: str) -> str:
    parsed = urlsplit(url)
    hosts = {value.strip().lower() for value in os.getenv("WEBHOOK_ALLOWED_HOSTS", "").split(",") if value.strip()}
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment or parsed.port not in {None, 443}:
        raise ValueError("Webhook exige uma URL HTTPS sem credenciais, na porta 443.")
    if parsed.hostname.lower() not in hosts:
        raise ValueError("Destino não autorizado. Cadastre o domínio em WEBHOOK_ALLOWED_HOSTS no servidor.")
    return url


def create_webhook(session, settings, *, owner_id: int, name: str, url: str) -> tuple[WebhookEndpoint, str]:
    if not name.strip() or len(name) > 120:
        raise ValueError("Informe um nome de até 120 caracteres.")
    url = validate_webhook_url(url)
    endpoint = WebhookEndpoint(owner_id=owner_id, name=name.strip(), url=url, signing_secret_ciphertext=b"", enabled=True)
    session.add(endpoint)
    session.flush()
    secret = secrets.token_urlsafe(32)
    endpoint.signing_secret_ciphertext = SecretCipher(settings.master_key).encrypt(secret, context=f"webhook:{endpoint.id}")
    return endpoint, secret


def enqueue_webhooks(session, limit: int = 50) -> int:
    total = 0
    for kind in ("job", "export"):
        key = func.concat("webhook:", kind, ":", Job.id if kind == "job" else ExportArtifact.id, ":v", Job.result_version if kind == "job" else ExportArtifact.result_version, ":", WebhookEndpoint.id)
        existing = select(NotificationOutbox.id).where(NotificationOutbox.deduplication_key == key).correlate(Job, WebhookEndpoint, *([ExportArtifact] if kind == "export" else [])).exists()
        statement = select(Job, WebhookEndpoint).join(WebhookEndpoint, WebhookEndpoint.owner_id == Job.requested_by_id).where(WebhookEndpoint.enabled.is_(True), ~existing)
        if kind == "job":
            statement = statement.where(Job.status.in_({"completed", "completed_with_errors", "failed", "cancelled"}))
        else:
            statement = statement.add_columns(ExportArtifact).join(ExportArtifact, ExportArtifact.job_id == Job.id).where(ExportArtifact.status == "ready")
        # Advisory lock makes enqueuing idempotent across scheduler instances.
        if not session.scalar(select(func.pg_try_advisory_xact_lock(87004211))):
            return total
        for row in session.execute(statement.order_by(Job.id).limit(limit)):
            job, endpoint = row[0], row[1]
            artifact = row[2] if kind == "export" else None
            entity_id = artifact.id if artifact else job.id
            version = artifact.result_version if artifact else job.result_version
            dedup = f"webhook:{kind}:{entity_id}:v{version}:{endpoint.id}"
            payload = {"event": "export.ready" if artifact else "job.finished", "job_id": job.id, "result_version": version, "status": job.status, "municipality_slug": job.municipality_slug}
            if artifact:
                payload.update({"export_id": artifact.id, "format": artifact.format, "sha256": artifact.sha256, "download_path": f"/api/v1/exports/{artifact.id}/download"})
            session.add(NotificationOutbox(deduplication_key=dedup, job_id=job.id, channel="webhook", recipient=str(endpoint.id), status="pending", payload_json=payload, attempts=0, max_attempts=5))
            total += 1
        session.flush()
    return total


def _post_pinned(url: str, body: bytes, headers: dict[str, str]) -> int:
    """Resolve once and connect to that validated IP with the original TLS SNI."""
    parsed = urlsplit(validate_webhook_url(url))
    hostname = parsed.hostname
    addresses = list(dict.fromkeys(answer[4][0] for answer in socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)))
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ValueError("Webhook não pode usar endereço privado ou reservado.")

    class PinnedHTTPSConnection(http.client.HTTPSConnection):
        def connect(self):
            raw = socket.create_connection((addresses[0], 443), self.timeout)
            try:
                self.sock = self._context.wrap_socket(raw, server_hostname=hostname)
            except BaseException:
                raw.close()
                raise

    connection = PinnedHTTPSConnection(hostname, timeout=15)
    try:
        connection.request("POST", (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else ""), body=body, headers=headers)
        response = connection.getresponse()
        return response.status
    finally:
        connection.close()


def process_one_webhook(session_factory, settings) -> bool:
    now, lock = datetime.now(UTC), secrets.token_hex(24)
    with session_factory() as session:
        session.execute(update(NotificationOutbox).where(NotificationOutbox.channel == "webhook", NotificationOutbox.status == "processing", NotificationOutbox.locked_until <= now, NotificationOutbox.attempts >= NotificationOutbox.max_attempts).values(status="failed", locked_by=None, locked_until=None, last_error="Entrega interrompida na última tentativa; limite atingido."))
        message = session.scalar(select(NotificationOutbox).where(NotificationOutbox.channel == "webhook", NotificationOutbox.attempts < NotificationOutbox.max_attempts,
            or_(NotificationOutbox.status.in_({"pending", "retry"}), and_(NotificationOutbox.status == "processing", NotificationOutbox.locked_until <= now)),
            or_(NotificationOutbox.next_attempt_at.is_(None), NotificationOutbox.next_attempt_at <= now)).order_by(NotificationOutbox.id).limit(1).with_for_update(skip_locked=True))
        if not message:
            session.commit()
            return False
        try:
            endpoint = session.get(WebhookEndpoint, int(message.recipient or ""))
        except ValueError:
            message.status = "failed"
            message.last_error = "Destinatário de webhook inválido."
            session.commit()
            return True
        if not endpoint or not endpoint.enabled:
            message.status = "cancelled"
            message.last_error = "Webhook desativado."
            session.commit()
            return True
        message.status, message.locked_by, message.locked_until = "processing", lock, now + timedelta(seconds=120)
        message.attempts += 1
        message_id = message.id
        url = endpoint.url
        secret = SecretCipher(settings.master_key).decrypt(endpoint.signing_secret_ciphertext, context=f"webhook:{endpoint.id}")
        body = json.dumps({"id": message.deduplication_key, **message.payload_json}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        timestamp = str(int(now.timestamp()))
        signature = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
        headers = {"Content-Type": "application/json", "X-Machine-Signature": f"t={timestamp},v1={signature}", "Idempotency-Key": message.deduplication_key, "User-Agent": "Machine-Webhooks/1"}
        session.commit()
    try:
        status_code = _post_pinned(url, body, headers)
        success = 200 <= status_code < 300
        error = None if success else f"Destino respondeu HTTP {status_code}."
    except Exception:
        success, error = False, "Não foi possível entregar ao destino HTTPS autorizado."
    with session_factory() as session:
        message = session.scalar(select(NotificationOutbox).where(NotificationOutbox.id == message_id, NotificationOutbox.locked_by == lock).with_for_update())
        if message:
            message.status = "sent" if success else ("failed" if message.attempts >= message.max_attempts else "retry")
            message.sent_at = datetime.now(UTC) if success else None
            message.next_attempt_at = None if success else datetime.now(UTC) + timedelta(seconds=min(3600, 30 * 2 ** message.attempts))
            message.last_error, message.locked_until, message.locked_by = error, None, None
            session.commit()
    return True

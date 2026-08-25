"""Outbox durável para entregas externas.

Concluir uma consulta nunca deve depender do Telegram. A transação apenas cria
uma mensagem idempotente; este módulo a entrega e agenda novas tentativas.
"""

from __future__ import annotations

import tempfile
import threading
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from machine_admin.config import Settings
from machine_admin.db import get_session_factory
from machine_admin.exports import build_job_export, job_export_filename
from machine_admin.models import Job, JobEvent, Municipality, NotificationOutbox
from services.telegram import TelegramNotifier


def enqueue_job_result(session: Session, job: Job) -> NotificationOutbox | None:
    """Agenda o resultado somente quando o job possui destinatário explícito."""
    if not job.telegram_chat_id:
        return None
    completion_version = int(
        (job.finished_at or datetime.now(UTC)).timestamp() * 1_000_000
    )
    key = f"telegram:job-result:{job.id}:{completion_version}"
    existing = session.scalar(
        select(NotificationOutbox).where(NotificationOutbox.deduplication_key == key)
    )
    if existing:
        return existing
    municipality = (
        session.get(Municipality, job.municipality_slug)
        if hasattr(session, "get")
        else None
    )
    message = NotificationOutbox(
        deduplication_key=key,
        job_id=job.id,
        channel="telegram",
        recipient=str(job.telegram_chat_id),
        status="pending",
        payload_json={
            "type": "job_result",
            "caption": f"Resultado final — {job.municipality_slug} (job #{job.id})",
            "filename": job_export_filename(
                municipality.name if municipality else job.municipality_slug,
                exported_at=job.finished_at or datetime.now(UTC),
                timezone_name=(
                    municipality.timezone
                    if municipality
                    else "America/Fortaleza"
                ),
            ),
        },
        max_attempts=5,
    )
    session.add(message)
    session.flush()
    return message


def claim_notification(
    session: Session,
    *,
    worker_id: str,
    lease_seconds: int = 900,
) -> NotificationOutbox | None:
    now = datetime.now(UTC)
    notification = session.scalar(
        select(NotificationOutbox)
        .where(
            or_(
                NotificationOutbox.status.in_(["pending", "retry"]),
                and_(
                    NotificationOutbox.status == "processing",
                    NotificationOutbox.locked_until <= now,
                ),
            ),
            NotificationOutbox.attempts < NotificationOutbox.max_attempts,
            or_(
                NotificationOutbox.next_attempt_at.is_(None),
                NotificationOutbox.next_attempt_at <= now,
            ),
            or_(
                NotificationOutbox.locked_until.is_(None),
                NotificationOutbox.locked_until <= now,
            ),
        )
        .order_by(NotificationOutbox.created_at, NotificationOutbox.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if not notification:
        return None
    notification.status = "processing"
    notification.locked_by = worker_id
    notification.locked_until = now + timedelta(seconds=lease_seconds)
    notification.attempts += 1
    session.flush()
    return notification


@contextmanager
def maintain_notification_lease(
    notification_id: int,
    worker_id: str,
    *,
    lease_seconds: int = 900,
    interval_seconds: int = 60,
):
    """Renova o lease enquanto a planilha é gerada e enviada."""
    stop_event = threading.Event()
    lost_lease = threading.Event()

    def renew() -> None:
        factory = get_session_factory()
        while not stop_event.wait(interval_seconds):
            try:
                with factory() as heartbeat_session:
                    cursor = heartbeat_session.execute(
                        update(NotificationOutbox)
                        .where(
                            NotificationOutbox.id == notification_id,
                            NotificationOutbox.status == "processing",
                            NotificationOutbox.locked_by == worker_id,
                        )
                        .values(
                            locked_until=datetime.now(UTC)
                            + timedelta(seconds=lease_seconds)
                        )
                    )
                    heartbeat_session.commit()
                    if cursor.rowcount != 1:
                        lost_lease.set()
                        return
            except Exception:
                # O lease inicial é longo; a próxima renovação pode recuperar.
                continue

    thread = threading.Thread(
        target=renew,
        name=f"notification-lease-{notification_id}",
        daemon=True,
    )
    thread.start()
    try:
        yield lost_lease
    finally:
        stop_event.set()
        thread.join(timeout=5)


def _mark_failure(notification: NotificationOutbox, error: Exception) -> None:
    notification.last_error = str(error)[:1000]
    notification.locked_by = None
    notification.locked_until = None
    if notification.attempts >= notification.max_attempts:
        notification.status = "failed"
        notification.next_attempt_at = None
        return
    notification.status = "retry"
    delay = min(30 * (2 ** max(notification.attempts - 1, 0)), 3600)
    notification.next_attempt_at = datetime.now(UTC) + timedelta(seconds=delay)


def deliver_notification(
    session: Session,
    settings: Settings,
    notification: NotificationOutbox,
) -> None:
    """Entrega uma mensagem já reservada e atualiza seu estado."""
    try:
        if notification.status != "processing" or not notification.locked_by:
            raise ValueError("Notificação sem lease de processamento válido.")
        if notification.channel != "telegram":
            raise ValueError(f"Canal não implementado: {notification.channel}")
        if notification.payload_json.get("type") != "job_result" or not notification.job_id:
            raise ValueError("Payload de notificação inválido.")
        if not notification.recipient:
            raise ValueError("Resultado sem destinatário explícito.")

        lease_context = (
            maintain_notification_lease(notification.id, notification.locked_by)
            if isinstance(session, Session) and notification.id is not None
            else nullcontext(threading.Event())
        )
        with lease_context as lost_lease:
            workbook, _ = build_job_export(session, settings, notification.job_id)
            if lost_lease.is_set():
                raise RuntimeError("Lease da notificação foi perdido antes do envio.")
            notifier = TelegramNotifier.for_chat(int(notification.recipient))
            if not notifier.enabled:
                raise RuntimeError("Telegram não configurado para o destinatário do job.")

            filename = str(
                notification.payload_json.get("filename")
                or job_export_filename(
                    f"job-{notification.job_id}", exported_at=datetime.now(UTC)
                )
            )
            filename = Path(filename).name
            if not filename.lower().endswith(".xlsx"):
                filename += ".xlsx"
            caption = str(
                notification.payload_json.get("caption") or "Resultado final"
            )
            caption = f"{caption} · envio #{notification.id}"
            with tempfile.TemporaryDirectory(
                prefix="machine_notification_"
            ) as directory:
                temporary_path = Path(directory) / filename
                temporary_path.write_bytes(workbook)
                if not notifier.document(temporary_path, caption):
                    raise RuntimeError("Telegram recusou ou não concluiu o envio.")
            if lost_lease.is_set():
                raise RuntimeError("Lease da notificação foi perdido durante o envio.")

        notification.status = "sent"
        notification.sent_at = datetime.now(UTC)
        notification.next_attempt_at = None
        notification.locked_by = None
        notification.locked_until = None
        notification.last_error = None
        if notification.job_id:
            session.add(
                JobEvent(
                    job_id=notification.job_id,
                    event_type="notification.sent",
                    message="Resultado enviado ao Telegram.",
                    event_data={"notification_id": notification.id},
                )
            )
    except Exception as exc:
        _mark_failure(notification, exc)
        if notification.job_id:
            session.add(
                JobEvent(
                    job_id=notification.job_id,
                    event_type="notification.error",
                    message=str(exc)[:500],
                    event_data={
                        "notification_id": notification.id,
                        "attempt": notification.attempts,
                        "final": notification.status == "failed",
                    },
                )
            )
        raise
    finally:
        session.flush()

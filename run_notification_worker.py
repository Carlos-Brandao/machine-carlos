"""Processa a outbox de notificações sem bloquear workers de consulta."""

from __future__ import annotations

import logging
import os
import signal
import socket
import threading
from pathlib import Path

from dotenv import load_dotenv

from machine_admin.db import get_session_factory, get_settings
from machine_admin.models import NotificationOutbox
from machine_admin.notifications import claim_notification, deliver_notification


LOG = logging.getLogger(__name__)


def main() -> None:
    load_dotenv(Path(__file__).parent / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    stop_event = threading.Event()

    def stop(*_: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    worker_id = f"notifications-{socket.gethostname()}-{os.getpid()}"
    factory = get_session_factory()
    settings = get_settings()

    while not stop_event.is_set():
        notification_id: int | None = None
        with factory() as session:
            notification = claim_notification(session, worker_id=worker_id)
            if notification:
                notification_id = notification.id
            session.commit()
        if notification_id is None:
            stop_event.wait(5)
            continue
        with factory() as session:
            notification = session.get(NotificationOutbox, notification_id)
            if not notification or notification.locked_by != worker_id:
                continue
            try:
                deliver_notification(session, settings, notification)
            except Exception as exc:
                LOG.warning("Falha na notificação %s: %s", notification_id, exc)
            session.commit()


if __name__ == "__main__":
    main()

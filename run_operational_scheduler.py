"""Durable scheduling, queue maintenance, export rendering and signed webhooks.

Run one or more replicas. PostgreSQL locks and unique keys coordinate ownership.
No portal or captcha calls are made by this process.
"""
from __future__ import annotations
import argparse
import logging
import signal
import threading
from dotenv import load_dotenv
from machine_admin.access_checks import expire_access_checks
from machine_admin.db import get_session_factory, get_settings
from machine_admin.product_exports import enqueue_final_exports, process_one_export
from machine_admin.queue import process_job_maintenance
from machine_admin.scheduling import process_due_schedules
from machine_admin.webhooks import enqueue_webhooks, process_one_webhook

LOG = logging.getLogger("operational-scheduler")


def maintenance_tick(session_factory, settings) -> dict[str, int]:
    counts = {}
    tasks = {"queue": process_job_maintenance, "access_checks": expire_access_checks,
        "schedules": process_due_schedules,
        "export_requests": lambda session: enqueue_final_exports(session, settings),
        "webhook_requests": enqueue_webhooks}
    for name, operation in tasks.items():
        try:
            with session_factory() as session:
                counts[name] = operation(session) or 0
                session.commit()
        except Exception as exc:
            # One invalid schedule/export must not prevent recovery of other jobs.
            LOG.error("%s failed (%s)", name, type(exc).__name__)
            counts[name] = -1
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings, factory = get_settings(), get_session_factory()
    if args.once:
        counts = maintenance_tick(factory, settings)
        process_one_export(factory, settings)
        process_one_webhook(factory, settings)
        if any(value == -1 for value in counts.values()):
            raise SystemExit(1)
        return
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())

    def consumer(operation, name):
        while not stop.is_set():
            try:
                did_work = operation(factory, settings)
            except Exception as exc:
                LOG.error("%s failed (%s)", name, type(exc).__name__)
                did_work = False
            if not did_work:
                stop.wait(5)

    threads = [threading.Thread(target=consumer, args=(operation, name), name=name, daemon=True)
        for operation, name in ((process_one_export, "exports"), (process_one_webhook, "webhooks"))]
    for thread in threads:
        thread.start()
    LOG.info("Scheduler ready")
    while not stop.is_set():
        maintenance_tick(factory, settings)
        stop.wait(5)
    for thread in threads:
        thread.join(timeout=20)


if __name__ == "__main__":
    main()

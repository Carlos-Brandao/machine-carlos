"""Executa pools de workers conectados ao painel."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import threading
from pathlib import Path

from dotenv import load_dotenv

from machine_admin.secret_store import configure_remote_secret_provider
from workers.api_client import WorkerAPIClient, WorkerAPIError
from workers.engine import GenericWorker
from workers.registry import ADAPTERS, create_adapter, default_worker_count


def main() -> None:
    load_dotenv(Path(__file__).parent / ".env")
    parser = argparse.ArgumentParser(description="Pool de workers Machine")
    available = tuple(slug for slug, item in ADAPTERS.items() if item.available)
    parser.add_argument("platform", choices=available)
    parser.add_argument("--workers", type=int)
    args = parser.parse_args()
    fixed_worker_count = (
        max(1, min(args.workers, 20))
        if args.workers is not None
        else None
    )
    base_url = os.getenv(
        "WORKER_API_URL", os.getenv("BACKEND_API_URL", "http://127.0.0.1:8000")
    )
    # O token bootstrap vem do env mínimo do serviço. Demais segredos são
    # obtidos sob demanda no backend, sem expor DB/master key ao navegador.
    token = os.getenv("WORKER_API_TOKEN", "").strip() or os.getenv(
        "BACKEND_API_TOKEN", ""
    ).strip()
    stop_event = threading.Event()
    slot_stops: dict[int, threading.Event] = {}
    threads: dict[int, threading.Thread] = {}

    def stop(*_: object) -> None:
        stop_event.set()
        for slot_stop in tuple(slot_stops.values()):
            slot_stop.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    identity = f"{socket.gethostname()}-{os.getpid()}"

    def run_slot(slot: int, slot_stop: threading.Event) -> None:
        GenericWorker(
            api=WorkerAPIClient(base_url, token),
            worker_id=f"{identity}-{slot}",
            stop_event=slot_stop,
            adapter=create_adapter(args.platform),
        ).run_forever()

    def start_slot(slot: int) -> None:
        slot_stop = threading.Event()
        thread = threading.Thread(
            target=run_slot,
            args=(slot, slot_stop),
            name=f"{args.platform}-worker-{slot}",
            daemon=False,
        )
        slot_stops[slot] = slot_stop
        threads[slot] = thread
        thread.start()

    controller = WorkerAPIClient(base_url, token)
    configure_remote_secret_provider(controller.runtime_secret)
    desired = fixed_worker_count or 1
    while not stop_event.is_set():
        for slot, thread in tuple(threads.items()):
            if thread.is_alive():
                continue
            thread.join()
            threads.pop(slot, None)
            slot_stops.pop(slot, None)
        if fixed_worker_count is None:
            try:
                capacity = controller.request(
                    "GET",
                    f"/api/workers/capacity?platform={args.platform}",
                )
                desired = max(0, min(int(capacity.get("desired_workers", 0)), 20))
            except (WorkerAPIError, TypeError, ValueError) as exc:
                # Sem API o pool não conseguiria trabalhar de qualquer forma;
                # preserve os slots existentes e tente novamente em seguida.
                desired = len(threads) or min(default_worker_count(args.platform), 1)
                logging.warning("Capacidade dinâmica indisponível: %s", exc)
        for slot in sorted(threads, reverse=True):
            if len(threads) <= desired:
                break
            slot_stops[slot].set()
        used_slots = set(threads)
        while len(threads) < desired:
            slot = next(number for number in range(1, 21) if number not in used_slots)
            used_slots.add(slot)
            start_slot(slot)
        stop_event.wait(30)

    stop()
    for thread in threads.values():
        thread.join(timeout=30)


if __name__ == "__main__":
    main()

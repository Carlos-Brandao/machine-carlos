"""Executa o pool de workers RF1 conectados ao painel."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dotenv import load_dotenv

from machine_admin.secret_store import get_runtime_secret
from workers.api_client import WorkerAPIClient
from workers.rf1_worker import RF1Worker


def main() -> None:
    load_dotenv(Path(__file__).parent / ".env")
    parser = argparse.ArgumentParser(description="Pool de workers Machine")
    parser.add_argument("platform", choices=["rf1"])
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args()
    worker_count = max(1, min(args.workers, 3))
    base_url = os.getenv(
        "WORKER_API_URL", os.getenv("BACKEND_API_URL", "http://127.0.0.1:8000")
    )
    token = get_runtime_secret("WORKER_API_TOKEN") or get_runtime_secret(
        "BACKEND_API_TOKEN"
    )
    stop_event = threading.Event()

    def stop(*_: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    identity = f"{socket.gethostname()}-{os.getpid()}"
    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures = [
            pool.submit(
                RF1Worker(
                    api=WorkerAPIClient(base_url, token),
                    worker_id=f"{identity}-{slot}",
                    stop_event=stop_event,
                ).run_forever
            )
            for slot in range(1, worker_count + 1)
        ]
        for future in futures:
            future.result()


if __name__ == "__main__":
    main()

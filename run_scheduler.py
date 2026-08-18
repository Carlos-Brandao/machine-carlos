"""Supervisor local compatível para os pools transacionais habilitados.

Em produção, systemd inicia uma unidade independente por plataforma. Este
processo permanece útil em desenvolvimento e reinicia somente o filho que caiu.
"""

from __future__ import annotations

import signal
import subprocess
import sys
import threading
from pathlib import Path

from dotenv import load_dotenv

from workers.registry import configured_platforms


def main() -> None:
    root = Path(__file__).parent
    load_dotenv(root / ".env")
    platforms = configured_platforms()
    if not platforms:
        raise SystemExit("Nenhum adapter transacional foi habilitado.")
    stop_event = threading.Event()
    children: dict[str, subprocess.Popen] = {}

    def start(platform: str) -> subprocess.Popen:
        return subprocess.Popen(
            [
                sys.executable,
                "-u",
                "run_worker.py",
                platform,
            ],
            cwd=root,
        )

    def stop(*_: object) -> None:
        stop_event.set()
        for child in children.values():
            if child.poll() is None:
                child.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    for platform in platforms:
        children[platform] = start(platform)

    try:
        while not stop_event.wait(1):
            for platform, child in tuple(children.items()):
                status = child.poll()
                if status is None:
                    continue
                print(
                    f"Pool {platform} encerrou com código {status}; reiniciando em 5s.",
                    flush=True,
                )
                if stop_event.wait(5):
                    break
                children[platform] = start(platform)
    finally:
        stop()
        for child in children.values():
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


if __name__ == "__main__":
    main()

"""Supervisor dos pools de consulta habilitados na VPS.

Cada portal possui seu próprio processo para que uma falha, captcha ou sessão
do RF1 não deixe os jobs FACILCONSIG presos em fila (e vice-versa).
"""

from __future__ import annotations

import signal
import subprocess
import sys
from pathlib import Path


# ConsigX permanece fora deste supervisor enquanto sua integração é revisada.
POOLS = (("rf1", 1), ("facil", 1))


def main() -> None:
    root = Path(__file__).parent
    children = [
        subprocess.Popen(
            [sys.executable, "-u", "run_worker.py", platform, "--workers", str(workers)],
            cwd=root,
        )
        for platform, workers in POOLS
    ]

    def stop(*_: object) -> None:
        for child in children:
            if child.poll() is None:
                child.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    statuses = [child.wait() for child in children]
    if any(status != 0 for status in statuses):
        raise SystemExit(next(status for status in statuses if status != 0))


if __name__ == "__main__":
    main()

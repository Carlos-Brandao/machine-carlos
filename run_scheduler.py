"""Inicializa o worker da fila de robôs."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from services.job_scheduler import JobScheduler


ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

database_path = Path(os.getenv("BACKEND_DATABASE_PATH", ROOT / "backend.sqlite3"))
JobScheduler(database_path, ROOT).run_forever()

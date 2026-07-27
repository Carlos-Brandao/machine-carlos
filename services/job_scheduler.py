"""Worker local que inicia jobs elegíveis da fila, no máximo três por vez."""

from __future__ import annotations

import logging
import os
import shlex
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from time import sleep

from services.scheduling import is_within_window


LOG = logging.getLogger(__name__)
MAX_CONCURRENCY = 3


class JobScheduler:
    def __init__(self, database_path: Path, root: Path, poll_seconds: int = 5) -> None:
        self.database_path = database_path
        self.root = root
        self.poll_seconds = poll_seconds
        self.log_dir = root / "job_logs"
        self.log_dir.mkdir(exist_ok=True)
        self.running: dict[int, subprocess.Popen[str]] = {}

    def run_forever(self) -> None:
        LOG.info("Scheduler iniciado com limite de %s robôs simultâneos.", MAX_CONCURRENCY)
        self._requeue_interrupted_jobs()
        while True:
            self._collect_finished()
            self._start_available_jobs()
            sleep(self.poll_seconds)

    def _connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _requeue_interrupted_jobs(self) -> None:
        """Após reinício do serviço, jobs sem processo ativo retornam à fila."""
        with self._connection() as connection:
            connection.execute(
                "UPDATE jobs SET status = 'queued', started_at = NULL WHERE status = 'running'"
            )

    def _collect_finished(self) -> None:
        for job_id, process in list(self.running.items()):
            return_code = process.poll()
            if return_code is None:
                continue
            status = "completed" if return_code == 0 else "failed"
            error = None if return_code == 0 else f"Processo encerrou com código {return_code}."
            with self._connection() as connection:
                connection.execute(
                    """
                    UPDATE jobs
                    SET status = ?, finished_at = ?, error_message = ?
                    WHERE id = ?
                    """,
                    (status, datetime.now(UTC).isoformat(), error, job_id),
                )
            del self.running[job_id]
            LOG.info("Job %s finalizado: %s", job_id, status)

    def _start_available_jobs(self) -> None:
        slots = MAX_CONCURRENCY - len(self.running)
        if slots <= 0:
            return

        now = datetime.now(UTC).isoformat()
        with self._connection() as connection:
            candidates = connection.execute(
                """
                SELECT id, prefeitura, platform
                FROM jobs
                WHERE status = 'queued' AND (not_before = '' OR not_before <= ?)
                ORDER BY created_at ASC
                """,
                (now,),
            ).fetchall()

        for job in candidates:
            if slots <= 0:
                break
            job_id = int(job["id"])
            platform = str(job["platform"])
            if platform == "unknown" or not is_within_window(platform):
                continue
            command = self._command_for(str(job["prefeitura"]))
            if not command:
                LOG.warning("Job %s aguardando configuração de comando para %s.", job_id, job["prefeitura"])
                continue
            if self._claim(job_id):
                self._launch(job_id, command)
                slots -= 1

    def _claim(self, job_id: int) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET status = 'running', started_at = ?
                WHERE id = ? AND status = 'queued'
                """,
                (datetime.now(UTC).isoformat(), job_id),
            )
            return cursor.rowcount == 1

    def _launch(self, job_id: int, command: list[str]) -> None:
        log_path = self.log_dir / f"job_{job_id}.log"
        try:
            log_file = log_path.open("a", encoding="utf-8")
            process = subprocess.Popen(
                command,
                cwd=self.root,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except OSError as exc:
            with self._connection() as connection:
                connection.execute(
                    """
                    UPDATE jobs SET status = 'failed', finished_at = ?, error_message = ?
                    WHERE id = ?
                    """,
                    (datetime.now(UTC).isoformat(), str(exc), job_id),
                )
            LOG.exception("Não foi possível iniciar job %s", job_id)
            return
        self.running[job_id] = process
        LOG.info("Job %s iniciado (pid=%s).", job_id, process.pid)

    @staticmethod
    def _command_for(prefeitura: str) -> list[str] | None:
        key = "ROBOT_COMMAND_" + prefeitura.upper().replace("-", "_")
        raw_command = os.getenv(key, "").strip()
        if raw_command:
            return shlex.split(raw_command)

        try:
            import sys
            from services.scheduling import platform_for
            platform = platform_for(prefeitura)
            return [sys.executable, "main.py", platform, prefeitura, "--yes"]
        except Exception:
            return None

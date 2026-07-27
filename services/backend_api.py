"""API interna para receber a fila solicitada pelo bot Telegram.

O serviço mantém os jobs em SQLite e exige um token Bearer em todas as rotas
operacionais. A execução dos robôs será conectada ao scheduler em seguida; por
enquanto esta API é a fonte persistente da fila e do status exposto ao bot.
"""

from __future__ import annotations

import hmac
import json
import os
import sqlite3
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from services.scheduling import (
    END_HOUR_BY_PLATFORM,
    START_HOUR,
    is_within_window,
    next_start_time,
    platform_for,
)


PREFEITURAS = {
    "itabuna": "Itabuna (Consiglog)",
    "fortaleza": "Fortaleza",
    "maranguape": "Maranguape",
    "tamboril": "Tamboril",
    "paulista": "Paulista",
    "paulista-previdencia": "Paulista Previdência",
    "boa-vista": "Boa Vista",
    "pref2": "pref2",
    "chapeco": "Chapecó",
    "teresina": "Teresina",
    "gov-am": "GOV AM",
    "mossoro": "Mossoró",
}


class BackendAPI:
    def __init__(self, database_path: Path, token: str, max_concurrency: int = 3) -> None:
        if not token:
            raise ValueError("BACKEND_API_TOKEN não configurado.")
        self.database_path = database_path
        self.token = token
        self.max_concurrency = max_concurrency
        self._initialize_database()

    def _connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize_database(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    prefeitura TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'completed', 'failed', 'cancelled')),
                    requested_by_user_id INTEGER NOT NULL,
                    requested_by_chat_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    not_before TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    error_message TEXT
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS jobs_status_created ON jobs(status, created_at)"
            )
            self._add_missing_columns(connection)
            for prefeitura in PREFEITURAS:
                connection.execute(
                    "UPDATE jobs SET platform = ? WHERE prefeitura = ? AND platform = 'unknown'",
                    (platform_for(prefeitura), prefeitura),
                )

    @staticmethod
    def _add_missing_columns(connection: sqlite3.Connection) -> None:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
        if "platform" not in columns:
            connection.execute("ALTER TABLE jobs ADD COLUMN platform TEXT NOT NULL DEFAULT 'unknown'")
        if "not_before" not in columns:
            connection.execute("ALTER TABLE jobs ADD COLUMN not_before TEXT NOT NULL DEFAULT ''")

    def create_batch(self, payload: dict[str, Any]) -> dict[str, Any]:
        requested = payload.get("prefeituras")
        requested_by = payload.get("requested_by")
        if not isinstance(requested, list) or not requested:
            raise ValueError("Informe ao menos uma prefeitura.")
        if not isinstance(requested_by, dict):
            raise ValueError("Dados do solicitante ausentes.")

        try:
            user_id = int(requested_by["telegram_user_id"])
            chat_id = int(requested_by["telegram_chat_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Dados do solicitante inválidos.") from exc

        slugs: list[str] = []
        invalid: list[str] = []
        for value in requested:
            slug = str(value).strip().lower()
            if slug not in PREFEITURAS:
                invalid.append(slug)
            elif slug not in slugs:
                slugs.append(slug)
        if invalid:
            raise ValueError("Prefeituras inválidas: " + ", ".join(invalid))

        now = datetime.now(UTC).isoformat()
        created: list[dict[str, Any]] = []
        skipped: list[str] = []
        with self._connection() as connection:
            for slug in slugs:
                platform = platform_for(slug)
                not_before = next_start_time(platform)
                existing = connection.execute(
                    "SELECT id FROM jobs WHERE prefeitura = ? AND status IN ('queued', 'running')",
                    (slug,),
                ).fetchone()
                if existing:
                    skipped.append(slug)
                    continue
                cursor = connection.execute(
                    """
                    INSERT INTO jobs(prefeitura, platform, status, requested_by_user_id, requested_by_chat_id, created_at, not_before)
                    VALUES (?, ?, 'queued', ?, ?, ?, ?)
                    """,
                    (slug, platform, user_id, chat_id, now, not_before.isoformat()),
                )
                created.append({
                    "id": cursor.lastrowid,
                    "prefeitura": slug,
                    "nome": PREFEITURAS[slug],
                    "platform": platform,
                    "not_before": not_before.isoformat(),
                })

        return {
            "created": created,
            "skipped": skipped,
            "message": f"{len(created)} base(s) adicionada(s) à fila.",
        }

    def status(self) -> dict[str, Any]:
        with self._connection() as connection:
            running = self._jobs(connection, "status = 'running'")
            queued = self._jobs(connection, "status = 'queued'")
            recent = self._jobs(connection, "status IN ('completed', 'failed', 'cancelled')", limit=10)
        return {
            "max_concurrency": self.max_concurrency,
            "running": running,
            "queued": queued,
            "queued_count": len(queued),
            "recent": recent,
            "scheduler": self._scheduler_status(),
        }

    def queue(self) -> dict[str, Any]:
        with self._connection() as connection:
            queued = self._jobs(connection, "status = 'queued'")
        return {"queued": queued, "count": len(queued)}

    @staticmethod
    def _scheduler_status() -> dict[str, Any]:
        return {
            "timezone": "America/Fortaleza",
            "weekdays_only": True,
            "start_hour": START_HOUR,
            "end_hour_by_platform": END_HOUR_BY_PLATFORM,
            "safeconsig_can_start_now": is_within_window("safeconsig"),
        }

    @staticmethod
    def _jobs(connection: sqlite3.Connection, where: str, limit: int | None = None) -> list[dict[str, Any]]:
        query = f"SELECT * FROM jobs WHERE {where} ORDER BY created_at ASC"
        if limit is not None:
            query += f" LIMIT {int(limit)}"
        return [dict(row) for row in connection.execute(query)]


def build_handler(api: BackendAPI):
    class Handler(BaseHTTPRequestHandler):
        server_version = "MachineBackend/1.0"

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/health":
                self._write_json(HTTPStatus.OK, {"ok": True})
                return
            if not self._authorized():
                return
            if self.path == "/api/jobs/status":
                self._write_json(HTTPStatus.OK, api.status())
            elif self.path == "/api/jobs/queue":
                self._write_json(HTTPStatus.OK, api.queue())
            else:
                self._write_json(HTTPStatus.NOT_FOUND, {"error": "Rota não encontrada."})

        def do_POST(self) -> None:  # noqa: N802
            if not self._authorized():
                return
            if self.path != "/api/jobs/batch":
                self._write_json(HTTPStatus.NOT_FOUND, {"error": "Rota não encontrada."})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 65_536:
                    raise ValueError("Corpo da requisição inválido.")
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("JSON inválido.")
                self._write_json(HTTPStatus.CREATED, api.create_batch(payload))
            except (ValueError, json.JSONDecodeError) as exc:
                self._write_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

        def _authorized(self) -> bool:
            expected = f"Bearer {api.token}"
            received = self.headers.get("Authorization", "")
            if hmac.compare_digest(received, expected):
                return True
            self._write_json(HTTPStatus.UNAUTHORIZED, {"error": "Não autorizado."})
            return False

        def _write_json(self, status: HTTPStatus, body: dict[str, Any]) -> None:
            content = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, format: str, *args: Any) -> None:
            print(f"[backend] {self.address_string()} - {format % args}", flush=True)

    return Handler


def serve() -> None:
    root = Path(__file__).resolve().parent.parent
    token = os.getenv("BACKEND_API_TOKEN", "").strip()
    host = os.getenv("BACKEND_HOST", "127.0.0.1")
    port = int(os.getenv("BACKEND_PORT", "8000"))
    database_path = Path(os.getenv("BACKEND_DATABASE_PATH", root / "backend.sqlite3"))
    api = BackendAPI(database_path, token)
    server = ThreadingHTTPServer((host, port), build_handler(api))
    print(f"Backend disponível em http://{host}:{port}", flush=True)
    server.serve_forever()

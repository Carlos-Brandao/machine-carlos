"""Immutable, encrypted export snapshots rendered by the operational scheduler."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import secrets
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

from openpyxl import Workbook
from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from machine_admin.config import Settings
from machine_admin.exports import _excel_safe, _export_row, _result_rows, job_export_filename
from machine_admin.models import ExportArtifact, Job, Municipality
from machine_admin.operations import TERMINAL_JOB_STATES
from machine_admin.security import SecretCipher

MEDIA_TYPES = {"xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "csv": "text/csv; charset=utf-8", "json": "application/json"}


def media_type_for(format: str) -> str:
    return MEDIA_TYPES[format]


def serialize_export(artifact: ExportArtifact) -> dict:
    fields = ("id", "job_id", "result_version", "format", "status", "filename", "sha256", "row_count", "partial", "size_bytes", "error_message")
    return {**{field: getattr(artifact, field) for field in fields},
        "created_at": artifact.created_at.isoformat() if artifact.created_at else None,
        "ready_at": artifact.ready_at.isoformat() if artifact.ready_at else None,
        "download_url": f"/api/v1/exports/{artifact.id}/download" if artifact.status == "ready" else None}


def request_export(session: Session, settings: Settings, job: Job, format: str = "xlsx") -> ExportArtifact:
    if format not in MEDIA_TYPES:
        raise ValueError("Formato disponível: xlsx, csv ou json.")
    # Serialize export requests with retry/control mutations of the same job.
    job = session.scalar(select(Job).where(Job.id == job.id).with_for_update())
    cipher = SecretCipher(settings.master_key)
    rows = [_export_row(cipher, item, record, result) for item, record, result in _result_rows(session, job.id)]
    # Preserve imported column order (CPF, MATRICULA, ...), including in later CSV/XLSX renders.
    snapshot = json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    snapshot_hash = hashlib.sha256(snapshot + b"\n" + job.status.encode()).hexdigest()
    version = job.result_version or 1
    artifact = session.scalar(select(ExportArtifact).where(ExportArtifact.job_id == job.id, ExportArtifact.result_version == version, ExportArtifact.snapshot_hash == snapshot_hash, ExportArtifact.format == format))
    if artifact:
        # Retrying generation reuses the frozen data, never mutable current results.
        if artifact.status == "failed":
            artifact.status = "queued"
            artifact.attempts = 0
            artifact.error_message = None
        return artifact
    municipality = session.get(Municipality, job.municipality_slug)
    filename = job_export_filename(municipality.name if municipality else job.municipality_slug, exported_at=datetime.now(UTC), timezone_name=municipality.timezone if municipality else "America/Fortaleza")
    artifact = ExportArtifact(job_id=job.id, result_version=version, snapshot_hash=snapshot_hash,
        snapshot_ciphertext=b"", format=format, status="queued",
        filename=str(Path(filename).with_suffix(f".{format}")), row_count=len(rows),
        partial=job.status not in TERMINAL_JOB_STATES, attempts=0)
    session.add(artifact)
    session.flush()
    artifact.snapshot_ciphertext = cipher.encrypt_bytes(zlib.compress(snapshot), context=f"export:{artifact.id}:snapshot")
    session.flush()
    return artifact


def render_snapshot(rows: list[dict], format: str) -> bytes:
    if format == "json":
        return json.dumps(rows, ensure_ascii=False, indent=2).encode("utf-8")
    columns = list(dict.fromkeys(key for row in rows for key in row))
    if format == "csv":
        output = io.StringIO(newline="")
        writer = csv.writer(output, delimiter=";")
        writer.writerow([_excel_safe(column) for column in columns])
        for row in rows:
            writer.writerow([_excel_safe(row.get(column)) for column in columns])
        return output.getvalue().encode("utf-8-sig")
    if format != "xlsx":
        raise ValueError("Formato de exportação inválido.")
    workbook = Workbook(write_only=True)
    worksheet = workbook.create_sheet("Resultados")
    worksheet.append([_excel_safe(column) for column in columns])
    for row in rows:
        worksheet.append([_excel_safe(row.get(column)) for column in columns])
    output_bytes = io.BytesIO()
    workbook.save(output_bytes)
    return output_bytes.getvalue()


def process_one_export(session_factory, settings: Settings) -> bool:
    token = secrets.token_hex(24)
    now = datetime.now(UTC)
    with session_factory() as session:
        session.execute(update(ExportArtifact).where(ExportArtifact.status == "building", ExportArtifact.locked_until <= now, ExportArtifact.attempts >= 3).values(status="failed", locked_by=None, locked_until=None, error_message="Geração interrompida repetidamente; solicite novamente para tentar com o mesmo snapshot."))
        artifact = session.scalar(select(ExportArtifact).where(
            or_(ExportArtifact.status == "queued", and_(ExportArtifact.status == "building", ExportArtifact.locked_until <= now)), ExportArtifact.attempts < 3
        ).order_by(ExportArtifact.id).limit(1).with_for_update(skip_locked=True))
        if not artifact:
            session.commit()
            return False
        artifact.status = "building"
        artifact.locked_by = token
        artifact.locked_until = now + timedelta(minutes=30)
        artifact.attempts += 1
        artifact_id, ciphertext, format = artifact.id, artifact.snapshot_ciphertext, artifact.format
        session.commit()
    try:
        cipher = SecretCipher(settings.master_key)
        snapshot = cipher.decrypt_bytes(ciphertext, context=f"export:{artifact_id}:snapshot")
        rows = json.loads(zlib.decompress(snapshot))
        payload = render_snapshot(rows, format)
        digest = hashlib.sha256(payload).hexdigest()
        directory = settings.storage_dir / "exports" / str(artifact_id)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        # The lock token makes old workers harmless even if a long render loses its lease.
        target = directory / f"{token}.enc"
        encrypted = cipher.encrypt_bytes(payload, context=f"export:{artifact_id}:file")
        with target.open("xb") as output:
            os.chmod(target, 0o600)
            output.write(encrypted)
        with session_factory() as session:
            artifact = session.scalar(select(ExportArtifact).where(ExportArtifact.id == artifact_id, ExportArtifact.locked_by == token, ExportArtifact.status == "building").with_for_update())
            if not artifact:
                target.unlink(missing_ok=True)
                return True
            artifact.storage_path = str(target)
            artifact.sha256 = digest
            artifact.size_bytes = len(payload)
            artifact.status = "ready"
            artifact.ready_at = datetime.now(UTC)
            artifact.locked_until = None
            artifact.locked_by = None
            artifact.error_message = None
            session.commit()
    except Exception:
        with session_factory() as session:
            artifact = session.scalar(select(ExportArtifact).where(ExportArtifact.id == artifact_id, ExportArtifact.locked_by == token).with_for_update())
            if artifact:
                artifact.status = "failed" if artifact.attempts >= 3 else "queued"
                artifact.error_message = "Não foi possível gerar o arquivo; tente solicitar a exportação novamente."
                artifact.locked_until = None
                artifact.locked_by = None
                session.commit()
        raise
    return True


def read_export(settings: Settings, artifact: ExportArtifact) -> bytes:
    if artifact.status != "ready" or not artifact.storage_path:
        raise ValueError("O arquivo ainda não está pronto.")
    path = Path(artifact.storage_path).resolve()
    if not path.is_relative_to((settings.storage_dir / "exports" / str(artifact.id)).resolve()):
        raise ValueError("Caminho de exportação inválido.")
    data = SecretCipher(settings.master_key).decrypt_bytes(path.read_bytes(), context=f"export:{artifact.id}:file")
    if hashlib.sha256(data).hexdigest() != artifact.sha256:
        raise ValueError("O arquivo não passou pela verificação de integridade.")
    return data


def enqueue_final_exports(session: Session, settings: Settings, limit: int = 5) -> int:
    # One preserved final XLSX per execution generation. Explicit requests can add formats.
    existing = select(ExportArtifact.id).where(ExportArtifact.job_id == Job.id, ExportArtifact.result_version == Job.result_version, ExportArtifact.format == "xlsx", ExportArtifact.partial.is_(False)).exists()
    jobs = list(session.scalars(select(Job).where(Job.status.in_({"completed", "completed_with_errors"}), ~existing).order_by(Job.finished_at, Job.id).limit(limit).with_for_update(skip_locked=True)))
    for job in jobs:
        try:
            with session.begin_nested():
                request_export(session, settings, job)
        except Exception:
            # A corrupted legacy result must not trap every subsequent final export.
            # Persist a visible failed request; a manual retry can create a new valid snapshot.
            session.add(ExportArtifact(job_id=job.id, result_version=job.result_version,
                snapshot_hash=hashlib.sha256(f"unreadable:{job.id}:{job.result_version}".encode()).hexdigest(),
                snapshot_ciphertext=b"", format="xlsx", status="failed",
                filename=job_export_filename(job.municipality_slug), row_count=0, partial=False,
                attempts=3, error_message="Um resultado desta execução não pôde ser lido. Revise a consulta e solicite a exportação novamente."))
    return len(jobs)

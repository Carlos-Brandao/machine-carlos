"""Importação cifrada de bases e criação idempotente de itens."""

from __future__ import annotations

import hashlib
import io
import json
import secrets
from pathlib import Path

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from machine_admin.config import Settings
from machine_admin.models import Dataset, DatasetRecord, Job, JobEvent, JobItem
from machine_admin.security import SecretCipher, fingerprint_identifier
from services.utils import digits_only


def _read_table(filename: str, payload: bytes) -> pd.DataFrame:
    suffix = Path(filename).suffix.lower()
    source = io.BytesIO(payload)
    if suffix == ".csv":
        return pd.read_csv(source, dtype=str)
    if suffix == ".xlsx":
        return pd.read_excel(source, dtype=str)
    raise ValueError("Formato não aceito. Envie XLSX ou CSV.")


def _validate_required_columns(columns: list[str]) -> tuple[str, str | None]:
    """Exige CPF como primeira coluna; matrícula é opcional na segunda."""
    normalized = [column.strip().lstrip("\ufeff").upper() for column in columns]
    if not normalized or normalized[0] != "CPF":
        raise ValueError("Formato inválido: a primeira coluna deve ser CPF.")
    registration_column = (
        columns[1] if len(columns) > 1 and normalized[1] == "MATRICULA" else None
    )
    return columns[0], registration_column


def normalise_custom_columns(value: str | list[str] | None) -> list[str]:
    """Normaliza campos definidos no painel sem alterar o schema SQL."""
    values = value.replace("\n", ",").split(",") if isinstance(value, str) else (value or [])
    columns = list(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))
    if len(columns) > 40:
        raise ValueError("Informe no máximo 40 campos personalizados.")
    if any(len(column) > 120 for column in columns):
        raise ValueError("Cada campo personalizado pode ter no máximo 120 caracteres.")
    return columns


def import_dataset(
    session: Session,
    settings: Settings,
    *,
    municipality_slug: str,
    filename: str,
    payload: bytes,
    uploaded_by_id: int,
    custom_columns: str | list[str] | None = None,
) -> Dataset:
    if not payload or len(payload) > settings.max_upload_bytes:
        raise ValueError("Arquivo vazio ou acima do limite permitido.")
    dataframe = _read_table(filename, payload)
    dataframe.columns = [str(column).strip() for column in dataframe.columns]
    cpf_column, registration_column = _validate_required_columns(list(dataframe.columns))
    extra_columns = normalise_custom_columns(custom_columns)
    digest = hashlib.sha256(payload).hexdigest()
    dataset = Dataset(
        municipality_slug=municipality_slug,
        uploaded_by_id=uploaded_by_id,
        original_filename=Path(filename).name,
        storage_path="pending",
        sha256=digest,
        row_count=0,
        custom_columns=extra_columns,
        status="uploading",
    )
    session.add(dataset)
    session.flush()

    cipher = SecretCipher(settings.master_key)
    directory = settings.storage_dir / "datasets" / str(dataset.id)
    directory.mkdir(parents=True, exist_ok=True)
    encrypted_path = directory / f"{digest}.enc"
    temporary_path = directory / f".{digest}.tmp"
    encrypted_file = cipher.encrypt_bytes(payload, context=f"dataset:{dataset.id}:file")
    temporary_path.write_bytes(encrypted_file)
    temporary_path.replace(encrypted_path)
    dataset.storage_path = str(encrypted_path)

    try:
        records: list[DatasetRecord] = []
        errors: list[int] = []
        for offset, (_, row) in enumerate(dataframe.iterrows(), start=2):
            digits = digits_only(row.get(cpf_column))
            if 8 <= len(digits) <= 11:
                digits = digits.zfill(11)
            if len(digits) != 11:
                errors.append(offset)
                continue
            context_id = secrets.token_hex(16)
            raw_row = {
                str(key): (None if pd.isna(value) else str(value))
                for key, value in row.to_dict().items()
            }
            for column in extra_columns:
                raw_row.setdefault(column, None)
            records.append(
                DatasetRecord(
                    dataset_id=dataset.id,
                    row_number=offset,
                    encryption_context=context_id,
                    cpf_ciphertext=cipher.encrypt(
                        digits, context=f"record:{context_id}:cpf"
                    ),
                    cpf_fingerprint=fingerprint_identifier(settings.master_key, digits),
                    cpf_last4=digits[-4:],
                    registration=(
                        str(row.get(registration_column)).strip()
                        if registration_column and not pd.isna(row.get(registration_column))
                        else None
                    ),
                    source_ciphertext=cipher.encrypt(
                        json.dumps(raw_row, ensure_ascii=False),
                        context=f"record:{context_id}:source",
                    ),
                    source_data={"columns": list(raw_row)},
                )
            )
        if errors:
            raise ValueError(
                f"{len(errors)} linha(s) com CPF inválido; primeiras linhas: "
                + ", ".join(map(str, errors[:10]))
            )
        if not records:
            raise ValueError("A base não possui registros válidos.")

        session.add_all(records)
        dataset.row_count = len(records)
        dataset.status = "ready"
        session.flush()
        return dataset
    except Exception:
        temporary_path.unlink(missing_ok=True)
        encrypted_path.unlink(missing_ok=True)
        raise


def delete_dataset_blob(storage_path: str | None) -> None:
    """Remove somente o blob cifrado criado por uma transação abortada."""
    if storage_path and storage_path != "pending":
        Path(storage_path).unlink(missing_ok=True)


def create_job_for_dataset(
    session: Session,
    *,
    dataset: Dataset,
    requested_by_id: int,
) -> Job:
    job = Job(
        municipality_slug=dataset.municipality_slug,
        dataset_id=dataset.id,
        requested_by_id=requested_by_id,
        status="queued",
        total_items=dataset.row_count,
    )
    session.add(job)
    session.flush()
    record_ids = session.scalars(
        select(DatasetRecord.id)
        .where(DatasetRecord.dataset_id == dataset.id)
        .order_by(DatasetRecord.id)
    )
    session.add_all(
        [JobItem(job_id=job.id, dataset_record_id=record_id) for record_id in record_ids]
    )
    session.add(
        JobEvent(
            job_id=job.id,
            event_type="dataset_attached",
            message=f"Base {dataset.id} vinculada com {dataset.row_count} itens.",
        )
    )
    session.flush()
    return job


def attach_dataset_to_job(session: Session, *, job: Job, dataset: Dataset) -> Job:
    if job.status != "awaiting_dataset":
        raise ValueError("O job não está aguardando uma base.")
    if job.municipality_slug != dataset.municipality_slug:
        raise ValueError("A base pertence a outro convênio.")
    job.dataset_id = dataset.id
    job.status = "queued"
    job.total_items = dataset.row_count
    record_ids = session.scalars(
        select(DatasetRecord.id).where(DatasetRecord.dataset_id == dataset.id)
    )
    session.add_all(
        [JobItem(job_id=job.id, dataset_record_id=record_id) for record_id in record_ids]
    )
    session.add(
        JobEvent(
            job_id=job.id,
            event_type="dataset_attached",
            message=f"Base {dataset.id} anexada pelo painel.",
        )
    )
    session.flush()
    return job

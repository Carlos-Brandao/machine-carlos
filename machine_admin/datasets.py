"""Importação cifrada de bases e criação idempotente de itens."""

from __future__ import annotations

import hashlib
import io
import json
import secrets
import zipfile
import csv
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
from openpyxl import load_workbook
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from machine_admin.config import Settings
from machine_admin.models import (
    Dataset,
    DatasetMembership,
    DatasetRecord,
    Job,
    JobEvent,
    JobItem,
    Municipality,
    Schedule,
)
from machine_admin.security import SecretCipher, fingerprint_identifier
from services.utils import digits_only


DUPLICATE_POLICIES = frozenset({"reject", "keep_first", "keep_all"})
DATASET_TYPES = {
    "efetivos": "Efetivos",
    "temporarios": "Temporários",
    "comissionados": "Comissionados",
    "geral": "Geral",
}
MAX_DATASET_ROWS = 250_000
MAX_DATASET_COLUMNS = 200
MAX_DATASET_CELLS = 2_000_000
MAX_CELL_CHARACTERS = 32_767
MAX_XLSX_UNCOMPRESSED_BYTES = 250 * 1024 * 1024


def _validate_xlsx_payload(payload: bytes) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            entries = archive.infolist()
            if len(entries) > 10_000:
                raise ValueError("O XLSX possui componentes demais.")
            if sum(entry.file_size for entry in entries) > MAX_XLSX_UNCOMPRESSED_BYTES:
                raise ValueError("O XLSX descompactado excede o limite de segurança.")
        workbook = load_workbook(io.BytesIO(payload), read_only=True, data_only=False)
        try:
            worksheet = workbook.active
            if worksheet.max_row > MAX_DATASET_ROWS + 1:
                raise ValueError(
                    f"A base pode ter no máximo {MAX_DATASET_ROWS:,} registros."
                )
            if worksheet.max_column > MAX_DATASET_COLUMNS:
                raise ValueError(
                    f"A base pode ter no máximo {MAX_DATASET_COLUMNS} colunas."
                )
            if worksheet.max_row * worksheet.max_column > MAX_DATASET_CELLS:
                raise ValueError(
                    "A base excede o limite de 2.000.000 de células. "
                    "Divida o arquivo em bases menores."
                )
        finally:
            workbook.close()
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("O arquivo XLSX está corrompido ou é inválido.") from exc


def _read_table(filename: str, payload: bytes) -> pd.DataFrame:
    suffix = Path(filename).suffix.lower()
    if suffix not in {".csv", ".xlsx"}:
        raise ValueError("Formato não aceito. Envie XLSX ou CSV.")
    if suffix == ".csv":
        estimated_rows = payload.count(b"\n") + 1
        if estimated_rows > MAX_DATASET_ROWS + 1:
            raise ValueError(
                f"A base pode ter no máximo {MAX_DATASET_ROWS:,} registros."
            )
        try:
            first_line = payload.splitlines()[0].decode("utf-8-sig")
            estimated_columns = len(next(csv.reader([first_line])))
            if estimated_columns > MAX_DATASET_COLUMNS:
                raise ValueError(
                    f"A base pode ter no máximo {MAX_DATASET_COLUMNS} colunas."
                )
            if estimated_rows * max(estimated_columns, 1) > MAX_DATASET_CELLS:
                raise ValueError(
                    "A base excede o limite de 2.000.000 de células. "
                    "Divida o arquivo em bases menores."
                )
            return pd.read_csv(io.BytesIO(payload), dtype=str)
        except ValueError as exc:
            if "A base " in str(exc):
                raise
            raise ValueError(
                "Não foi possível ler o CSV. Confirme a codificação e o formato do arquivo."
            ) from exc
        except Exception as exc:
            raise ValueError(
                "Não foi possível ler o CSV. Confirme a codificação e o formato do arquivo."
            ) from exc
    _validate_xlsx_payload(payload)
    try:
        return pd.read_excel(io.BytesIO(payload), dtype=str)
    except Exception as exc:
        raise ValueError(
            "Não foi possível ler o XLSX. Confirme se o arquivo não está corrompido."
        ) from exc


def _validate_required_columns(columns: list[str]) -> tuple[str, str | None]:
    """Exige CPF como primeira coluna; matrícula é opcional na segunda."""
    normalized = [column.strip().lstrip("\ufeff").upper() for column in columns]
    if not normalized or normalized[0] != "CPF":
        raise ValueError("Formato inválido: a primeira coluna deve ser CPF.")
    registration_column = (
        columns[1] if len(columns) > 1 and normalized[1] == "MATRICULA" else None
    )
    return columns[0], registration_column


def normalize_cpf(value: object) -> str | None:
    """Normaliza CPF e valida seus dois dígitos verificadores.

    Arquivos Excel frequentemente removem zeros iniciais de colunas numéricas;
    por isso valores com 8 a 10 dígitos continuam recebendo ``zfill`` antes da
    validação oficial.
    """
    digits = digits_only(value)
    if 8 <= len(digits) <= 10:
        digits = digits.zfill(11)
    if len(digits) != 11 or len(set(digits)) == 1:
        return None

    numbers = [int(char) for char in digits]

    def verifier(values: list[int], start_weight: int) -> int:
        remainder = sum(
            number * weight
            for number, weight in zip(values, range(start_weight, 1, -1))
        ) % 11
        return 0 if remainder < 2 else 11 - remainder

    first = verifier(numbers[:9], 10)
    second = verifier(numbers[:9] + [first], 11)
    return digits if numbers[9:] == [first, second] else None


def normalize_duplicate_policy(value: str | None) -> str:
    policy = (value or "keep_first").strip().lower()
    if policy not in DUPLICATE_POLICIES:
        raise ValueError(
            "Política de duplicados inválida. Use reject, keep_first ou keep_all."
        )
    return policy


def _record_identity(cpf: str, registration: str | None) -> tuple[str, str]:
    return cpf, (registration or "").strip().casefold()


def normalise_custom_columns(value: str | list[str] | None) -> list[str]:
    """Normaliza campos definidos no painel sem alterar o schema SQL."""
    values = value.replace("\n", ",").split(",") if isinstance(value, str) else (value or [])
    columns = list(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))
    if len(columns) > 40:
        raise ValueError("Informe no máximo 40 campos personalizados.")
    if any(len(column) > 120 for column in columns):
        raise ValueError("Cada campo personalizado pode ter no máximo 120 caracteres.")
    return columns


def normalize_dataset_type(value: str | None) -> str:
    dataset_type = (value or "geral").strip().lower()
    if dataset_type not in DATASET_TYPES:
        raise ValueError("Tipo inválido. Selecione Efetivos, Temporários, Comissionados ou Geral.")
    return dataset_type


def _catalog_lock_key(municipality_slug: str) -> int:
    return int.from_bytes(
        hashlib.sha256(f"dataset-catalog:{municipality_slug}".encode()).digest()[:8],
        "big", signed=True,
    )


def lock_dataset_catalog(session: Session, municipality_slug: str, *, wait: bool = True) -> bool:
    # Serializes catalog writes AND job snapshots, including creation of an absent type.
    if not wait:
        return try_lock_dataset_catalog(session, municipality_slug)
    session.execute(select(func.pg_advisory_xact_lock(_catalog_lock_key(municipality_slug))))
    return True


def try_lock_dataset_catalog(session: Session, municipality_slug: str) -> bool:
    """Avoid reversed lock waits when the scheduler already owns a schedule row."""
    return bool(session.scalar(select(func.pg_try_advisory_xact_lock(_catalog_lock_key(municipality_slug)))))


def _identity_key(cpf_fingerprint: str, registration: str | None, duplicate_key: list[str]) -> str:
    registration_key = (registration or "").strip().lower() if "registration" in duplicate_key else ""
    return f"{cpf_fingerprint}:{registration_key}"


def _input_rules(municipality: Municipality) -> tuple[set[str], list[str]]:
    schema = municipality.input_schema or {}
    return (
        {str(field).strip().lower() for field in schema.get("required", [])},
        [str(field).strip().lower() for field in schema.get("deduplication_key", ["cpf", "registration"])],
    )


def dataset_records_select(dataset_id: int):
    """Current catalog contents; never use record ownership as membership."""
    return (
        select(DatasetRecord)
        .join(DatasetMembership, DatasetMembership.dataset_record_id == DatasetRecord.id)
        .where(DatasetMembership.dataset_id == dataset_id)
        .order_by(DatasetMembership.id)
    )


def _memberships(session: Session, dataset_id: int) -> dict[str, int]:
    return dict(session.execute(
        select(DatasetMembership.identity_key, DatasetMembership.dataset_record_id)
        .where(DatasetMembership.dataset_id == dataset_id)
    ).all())


def _active_dataset(session: Session, municipality_slug: str, dataset_type: str) -> Dataset | None:
    return session.scalar(
        select(Dataset).where(
            Dataset.municipality_slug == municipality_slug,
            Dataset.dataset_type == dataset_type,
            Dataset.status != "archived",
        ).execution_options(populate_existing=True)
    )


def _new_dataset(
    session: Session, *, municipality_slug: str, dataset_type: str,
    display_name: str, uploaded_by_id: int | None, filename: str = "base-geral",
) -> Dataset:
    dataset = Dataset(
        municipality_slug=municipality_slug, dataset_type=dataset_type,
        uploaded_by_id=uploaded_by_id, original_filename=Path(filename).name,
        display_name=display_name, storage_path="generated",
        sha256=hashlib.sha256(f"generated:{municipality_slug}:{dataset_type}".encode()).hexdigest(),
        row_count=0, duplicate_policy="keep_first", metadata_json={},
        custom_columns=[], status="ready",
    )
    session.add(dataset)
    session.flush()
    return dataset


def _ensure_general(session: Session, municipality: Municipality, uploaded_by_id: int | None) -> Dataset:
    return _active_dataset(session, municipality.slug, "geral") or _new_dataset(
        session, municipality_slug=municipality.slug, dataset_type="geral",
        display_name=f"{municipality.name} — Geral"[:160], uploaded_by_id=uploaded_by_id,
    )


def _sync_general(session: Session, *, dataset: Dataset, general: Dataset) -> int:
    if dataset.id == general.id:
        return 0
    existing = _memberships(session, general.id)
    additions = [
        DatasetMembership(dataset_id=general.id, dataset_record_id=record_id, identity_key=identity)
        for identity, record_id in _memberships(session, dataset.id).items()
        if identity not in existing
    ]
    session.add_all(additions)
    general.row_count = len(existing) + len(additions)
    general.metadata_json = {**general.metadata_json, "last_complemented_by_dataset_id": dataset.id}
    session.flush()
    return len(additions)


def import_dataset(
    session: Session,
    settings: Settings,
    *,
    municipality_slug: str,
    filename: str,
    payload: bytes,
    uploaded_by_id: int,
    custom_columns: str | list[str] | None = None,
    display_name: str | None = None,
    dataset_type: str = "geral",
    duplicate_policy: str = "keep_first",
    metadata: dict[str, Any] | None = None,
) -> Dataset:
    if not payload or len(payload) > settings.max_upload_bytes:
        raise ValueError("Arquivo vazio ou acima do limite permitido.")
    dataframe = _read_table(filename, payload)
    dataframe.columns = [str(column).strip() for column in dataframe.columns]
    if len(dataframe) > MAX_DATASET_ROWS:
        raise ValueError(f"A base pode ter no máximo {MAX_DATASET_ROWS:,} registros.")
    if len(dataframe.columns) > MAX_DATASET_COLUMNS:
        raise ValueError(f"A base pode ter no máximo {MAX_DATASET_COLUMNS} colunas.")
    if len(dataframe) * max(len(dataframe.columns), 1) > MAX_DATASET_CELLS:
        raise ValueError(
            "A base excede o limite de 2.000.000 de células. "
            "Divida o arquivo em bases menores."
        )
    if any(len(column) > 120 for column in dataframe.columns):
        raise ValueError("Os nomes das colunas podem ter no máximo 120 caracteres.")
    cpf_column, registration_column = _validate_required_columns(list(dataframe.columns))
    municipality = session.get(Municipality, municipality_slug)
    if municipality is None:
        raise ValueError("Convênio não encontrado.")
    required_fields, duplicate_key = _input_rules(municipality)
    if "registration" in required_fields and registration_column is None:
        raise ValueError(
            "Formato inválido para este convênio: a segunda coluna deve ser MATRICULA."
        )
    extra_columns = normalise_custom_columns(custom_columns)
    # Kept in the Python signature for old callers; catalogs always add only missing identities.
    normalize_duplicate_policy(duplicate_policy)
    dataset_type = normalize_dataset_type(dataset_type)
    friendly_name = (display_name or Path(filename).stem).strip()
    if not friendly_name:
        raise ValueError("O nome amigável da base é obrigatório.")
    if len(friendly_name) > 160:
        raise ValueError("O nome amigável da base pode ter no máximo 160 caracteres.")
    # Validate the entire upload before locking or touching files/database records.
    valid_rows: list[tuple[str, str | None, dict[str, Any], str]] = []
    error_samples: list[int] = []
    missing_required_samples: list[int] = []
    error_count = missing_required_count = duplicate_count = 0
    seen: set[str] = set()
    for offset, (_, row) in enumerate(dataframe.iterrows(), start=2):
        digits = normalize_cpf(row.get(cpf_column))
        if digits is None:
            error_count += 1
            if len(error_samples) < 10:
                error_samples.append(offset)
            continue
        registration = (
            str(row.get(registration_column)).strip()
            if registration_column and not pd.isna(row.get(registration_column)) else None
        )
        if registration and len(registration) > 120:
            raise ValueError(f"A matrícula na linha {offset} excede 120 caracteres.")
        if "registration" in required_fields and not registration:
            missing_required_count += 1
            if len(missing_required_samples) < 10:
                missing_required_samples.append(offset)
            continue
        fingerprint = fingerprint_identifier(settings.master_key, digits)
        identity = _identity_key(fingerprint, registration, duplicate_key)
        if identity in seen:
            duplicate_count += 1
            continue
        seen.add(identity)
        raw_row = {
            str(key): (None if pd.isna(value) else str(value))
            for key, value in row.to_dict().items()
        }
        if any(len(value) > MAX_CELL_CHARACTERS for value in raw_row.values() if isinstance(value, str)):
            raise ValueError(f"A linha {offset} contém uma célula acima de {MAX_CELL_CHARACTERS} caracteres.")
        for column in extra_columns:
            raw_row.setdefault(column, None)
        valid_rows.append((digits, registration, raw_row, identity))
    if not valid_rows:
        raise ValueError("A base não possui registros válidos.")

    lock_dataset_catalog(session, municipality_slug)
    session.refresh(municipality)
    current_required, current_key = _input_rules(municipality)
    if current_required != required_fields or set(current_key) != set(duplicate_key):
        raise ValueError(
            "As regras de identificação deste convênio mudaram durante a importação. "
            "Envie novamente o arquivo."
        )
    dataset = _active_dataset(session, municipality_slug, dataset_type)
    created = dataset is None
    if dataset is None:
        dataset = _new_dataset(
            session, municipality_slug=municipality_slug, dataset_type=dataset_type,
            display_name=friendly_name, uploaded_by_id=uploaded_by_id, filename=filename,
        )
    general = dataset if dataset_type == "geral" else _ensure_general(session, municipality, uploaded_by_id)
    target_members = _memberships(session, dataset.id)
    reusable_members = target_members if general.id == dataset.id else _memberships(session, general.id)
    initial_count = len(target_members)
    existing_count = 0
    next_row = int(session.scalar(
        select(func.coalesce(func.max(DatasetRecord.row_number), 1))
        .where(DatasetRecord.dataset_id == dataset.id)
    ))
    cipher = SecretCipher(settings.master_key)
    digest = hashlib.sha256(payload).hexdigest()
    directory = settings.storage_dir / "datasets" / str(dataset.id)
    directory.mkdir(parents=True, exist_ok=True)
    # Re-importing the same file must never overwrite a committed source artifact.
    nonce = secrets.token_hex(8)
    encrypted_path = directory / f"{digest}-{nonce}.enc"
    temporary_path = directory / f".{digest}-{nonce}.tmp"
    dataset._import_blob_path = str(encrypted_path)
    try:
        temporary_path.write_bytes(cipher.encrypt_bytes(payload, context=f"dataset:{dataset.id}:file"))
        temporary_path.replace(encrypted_path)
        new_records: list[tuple[DatasetRecord, str]] = []

        def flush_records() -> None:
            if not new_records:
                return
            session.add_all([record for record, _ in new_records])
            session.flush()
            session.add_all([
                DatasetMembership(dataset_id=dataset.id, dataset_record_id=record.id, identity_key=identity)
                for record, identity in new_records
            ])
            session.flush()
            new_records.clear()

        for digits, registration, raw_row, identity in valid_rows:
            if identity in target_members:
                existing_count += 1
                continue
            if identity in reusable_members:
                session.add(DatasetMembership(
                    dataset_id=dataset.id, dataset_record_id=reusable_members[identity], identity_key=identity,
                ))
                target_members[identity] = reusable_members[identity]
                continue
            next_row += 1
            context_id = secrets.token_hex(16)
            record = DatasetRecord(
                dataset_id=dataset.id, row_number=next_row, encryption_context=context_id,
                cpf_ciphertext=cipher.encrypt(digits, context=f"record:{context_id}:cpf"),
                cpf_fingerprint=identity.split(":", 1)[0], cpf_last4=digits[-4:], registration=registration,
                source_ciphertext=cipher.encrypt(json.dumps(raw_row, ensure_ascii=False), context=f"record:{context_id}:source"),
                source_data={"columns": list(raw_row)},
            )
            new_records.append((record, identity))
            target_members[identity] = 0
            if len(new_records) >= 1_000:
                flush_records()
        flush_records()
        session.flush()
        dataset.row_count = len(target_members)
        dataset.status = "ready"
        dataset.duplicate_policy = "keep_first"
        dataset.custom_columns = list(dict.fromkeys([*dataset.custom_columns, *extra_columns]))
        dataset.storage_path = str(encrypted_path)
        dataset.original_filename = Path(filename).name
        dataset.sha256 = digest
        general_added = _sync_general(session, dataset=dataset, general=general)
        dataset.metadata_json = {
            **dataset.metadata_json, **(metadata or {}), "import_version": 3,
            "source_columns": list(dict.fromkeys([*dataset.metadata_json.get("source_columns", []), *dataframe.columns])),
            "cpf_validation": "checksum", "duplicate_key": duplicate_key,
            "invalid_row_count": error_count, "missing_required_row_count": missing_required_count,
            "duplicate_row_count": duplicate_count, "existing_row_count": existing_count,
            "added_row_count": dataset.row_count - initial_count, "general_added_row_count": general_added,
            "general_dataset_id": general.id, "import_action": "created" if created else "complemented",
            "last_imported_at": datetime.now(UTC).isoformat(), "last_uploaded_name": friendly_name,
        }
        warnings: list[str] = []
        if error_count:
            warnings.append(f"{error_count} linha(s) ignorada(s) por CPF inválido; primeiras linhas: " + ", ".join(map(str, error_samples)))
        if missing_required_count:
            warnings.append(f"{missing_required_count} linha(s) ignorada(s) por matrícula ausente; primeiras linhas: " + ", ".join(map(str, missing_required_samples)))
        if duplicate_count:
            warnings.append(f"{duplicate_count} linha(s) repetida(s) no arquivo foram ignoradas.")
        dataset.error_message = " ".join(warnings) or None
        session.flush()
        return dataset
    except Exception:
        temporary_path.unlink(missing_ok=True)
        encrypted_path.unlink(missing_ok=True)
        raise


def delete_dataset_blob(storage_path: str | None) -> None:
    """Remove somente o blob cifrado criado por uma transação abortada."""
    if storage_path and storage_path not in {"pending", "generated"}:
        Path(storage_path).unlink(missing_ok=True)


def cleanup_import_blob(dataset: Dataset | None) -> None:
    """Rollback cleanup never removes an older, committed upload for this base."""
    if dataset is not None:
        delete_dataset_blob(getattr(dataset, "_import_blob_path", None))


def update_dataset(
    session: Session, *, dataset: Dataset, display_name: str,
    dataset_type: str | None = None,
) -> Dataset:
    name = display_name.strip()
    if not name or len(name) > 160:
        raise ValueError("Informe um nome para a base com até 160 caracteres.")
    lock_dataset_catalog(session, dataset.municipality_slug)
    session.refresh(dataset)
    if dataset.status != "ready":
        raise ValueError("Apenas bases ativas e prontas podem ser editadas.")
    target_type = normalize_dataset_type(dataset_type) if dataset_type else dataset.dataset_type
    if target_type != dataset.dataset_type:
        existing = _active_dataset(session, dataset.municipality_slug, target_type)
        if existing is not None:
            raise ValueError(
                f"Já existe uma base {DATASET_TYPES[target_type]} neste convênio. "
                "Importe o arquivo nesse tipo para complementar a base existente."
            )
        municipality = session.get(Municipality, dataset.municipality_slug)
        session.refresh(municipality)
        if dataset.dataset_type is None:
            # Classification is an explicit catalog edit. Historic jobs keep
            # their original immutable records even when duplicate members go away.
            _, duplicate_key = _input_rules(municipality)
            unique: dict[str, int] = {}
            for record in session.scalars(dataset_records_select(dataset.id)):
                unique.setdefault(_identity_key(record.cpf_fingerprint, record.registration, duplicate_key), record.id)
            session.execute(delete(DatasetMembership).where(DatasetMembership.dataset_id == dataset.id))
            session.add_all([
                DatasetMembership(dataset_id=dataset.id, dataset_record_id=record_id, identity_key=identity)
                for identity, record_id in unique.items()
            ])
            dataset.metadata_json = {
                **dataset.metadata_json, "rows_before_classification": dataset.row_count,
                "classified_at": datetime.now(UTC).isoformat(), "duplicate_key": duplicate_key,
            }
            dataset.row_count = len(unique)
            dataset.duplicate_policy = "keep_first"
        dataset.dataset_type = target_type
        session.flush()
        if target_type != "geral":
            general = _ensure_general(session, municipality, dataset.uploaded_by_id)
            _sync_general(session, dataset=dataset, general=general)
    dataset.display_name = name
    session.flush()
    return dataset


def archive_dataset(session: Session, *, dataset: Dataset) -> Dataset:
    lock_dataset_catalog(session, dataset.municipality_slug)
    session.refresh(dataset)
    if dataset.status == "archived":
        return dataset
    if dataset.dataset_type == "geral":
        specific = session.scalar(select(Dataset.id).where(
            Dataset.municipality_slug == dataset.municipality_slug,
            Dataset.dataset_type != "geral", Dataset.status != "archived",
        ).limit(1))
        if specific is not None:
            raise ValueError("Remova primeiro as bases específicas deste convênio para remover a base Geral.")
    dataset.status = "archived"
    dataset.metadata_json = {**dataset.metadata_json, "archived_at": datetime.now(UTC).isoformat()}
    session.execute(update(Schedule).where(Schedule.dataset_id == dataset.id).values(enabled=False))
    session.flush()
    return dataset


def _job_snapshot(session: Session, dataset: Dataset) -> list[int]:
    lock_dataset_catalog(session, dataset.municipality_slug)
    session.refresh(dataset)
    if dataset.status != "ready":
        raise ValueError("Selecione uma base ativa e pronta para iniciar a consulta.")
    record_ids = list(session.scalars(
        select(DatasetMembership.dataset_record_id)
        .where(DatasetMembership.dataset_id == dataset.id)
        .order_by(DatasetMembership.id)
    ))
    if not record_ids:
        raise ValueError("A base não possui registros disponíveis para consulta.")
    return record_ids


def create_job_for_dataset(
    session: Session,
    *,
    dataset: Dataset,
    requested_by_id: int | None,
) -> Job:
    record_ids = _job_snapshot(session, dataset)
    job = Job(
        municipality_slug=dataset.municipality_slug,
        dataset_id=dataset.id,
        requested_by_id=requested_by_id,
        status="queued",
        total_items=len(record_ids),
    )
    session.add(job)
    session.flush()
    session.add_all(
        [JobItem(job_id=job.id, dataset_record_id=record_id) for record_id in record_ids]
    )
    session.add(
        JobEvent(
            job_id=job.id,
            event_type="dataset_attached",
            message=f"Base {dataset.id} vinculada com {len(record_ids)} itens.",
        )
    )
    session.flush()
    return job


def attach_dataset_to_job(session: Session, *, job: Job, dataset: Dataset) -> Job:
    if job.status != "awaiting_dataset":
        raise ValueError("O job não está aguardando uma base.")
    if job.municipality_slug != dataset.municipality_slug:
        raise ValueError("A base pertence a outro convênio.")
    record_ids = _job_snapshot(session, dataset)
    job.dataset_id = dataset.id
    job.status = "queued"
    job.total_items = len(record_ids)
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

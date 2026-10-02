"""Shared dataset representations for the administrative pages and API."""

import json

from machine_admin.datasets import DATASET_TYPES, dataset_records_select
from machine_admin.security import SecretCipher


def serialize_dataset(dataset):
    return {
        "id": dataset.id,
        "name": dataset.display_name or dataset.original_filename,
        "municipality_slug": dataset.municipality_slug,
        "dataset_type": dataset.dataset_type,
        "type_label": DATASET_TYPES.get(dataset.dataset_type, "Não classificada"),
        "row_count": dataset.row_count,
        "status": dataset.status,
        "warnings": dataset.error_message,
    }


def import_summary(dataset):
    metadata = dataset.metadata_json or {}
    return {key: metadata.get(key, 0) for key in (
        "added_row_count", "existing_row_count", "general_added_row_count",
        "duplicate_row_count", "invalid_row_count", "missing_required_row_count",
    )} | {"action": metadata.get("import_action", "created")}


def dataset_record_page(session, settings, dataset, *, page=1, limit=50):
    records = list(session.scalars(
        dataset_records_select(dataset.id).offset((page - 1) * limit).limit(limit + 1)
    ))
    cipher = SecretCipher(settings.master_key)
    items = []
    for record in records[:limit]:
        items.append({
            "id": record.id,
            "cpf": cipher.decrypt(record.cpf_ciphertext, context=f"record:{record.encryption_context}:cpf"),
            "registration": record.registration,
            "source": json.loads(cipher.decrypt(record.source_ciphertext, context=f"record:{record.encryption_context}:source")),
        })
    return {
        "items": items,
        "page": page,
        "limit": limit,
        "total": dataset.row_count,
        "has_next": len(records) > limit,
    }

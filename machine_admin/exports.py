"""Exportações estáveis dos resultados de consulta.

As colunas originais nunca podem ser sobrescritas pelo retorno do portal. O
contrato canônico ganha nomes próprios; campos legados ou específicos ficam no
namespace ``RETORNO_*``.
"""

from __future__ import annotations

import io
import json
import re
import unicodedata
from collections.abc import Mapping
from typing import Any

from openpyxl import Workbook
from sqlalchemy import select
from sqlalchemy.orm import Session

from machine_admin.config import Settings
from machine_admin.models import ConsultationResult, DatasetRecord, JobItem
from machine_admin.security import SecretCipher


def _column_fragment(value: object) -> str:
    normalized = unicodedata.normalize("NFKD", str(value))
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^A-Z0-9]+", "_", ascii_value.upper()).strip("_") or "CAMPO"


def _scalar(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _flatten_canonical_result(result: Mapping[str, Any]) -> dict[str, Any]:
    exported: dict[str, Any] = {}
    outcome = result.get("outcome") or result.get("status")
    if outcome is not None:
        exported["Resultado"] = _scalar(outcome)

    for section, prefix in (
        ("requested", "SOLICITADO"),
        ("confirmed", "CONFIRMADO"),
        ("person", "SERVIDOR"),
    ):
        values = result.get(section)
        if isinstance(values, Mapping):
            for key, value in values.items():
                exported[f"{prefix}_{_column_fragment(key)}"] = _scalar(value)

    margins = result.get("margins")
    if isinstance(margins, Mapping):
        for key, value in margins.items():
            margin_type = _column_fragment(key)
            if isinstance(value, Mapping):
                for detail_key, detail_value in value.items():
                    exported[
                        f"MARGEM_{margin_type}_{_column_fragment(detail_key)}"
                    ] = _scalar(detail_value)
            else:
                exported[f"MARGEM_{margin_type}"] = _scalar(value)
    elif isinstance(margins, list):
        for index, margin in enumerate(margins, start=1):
            if not isinstance(margin, Mapping):
                continue
            margin_type = _column_fragment(margin.get("type") or index)
            for key, value in margin.items():
                if key == "type":
                    continue
                exported[f"MARGEM_{margin_type}_{_column_fragment(key)}"] = _scalar(value)

    raw = result.get("raw")
    if isinstance(raw, Mapping):
        for key, value in raw.items():
            exported[f"RETORNO_{_column_fragment(key)}"] = _scalar(value)

    consumed = {"outcome", "status", "requested", "confirmed", "person", "margins", "raw"}
    for key, value in result.items():
        if key not in consumed:
            exported[f"RETORNO_{_column_fragment(key)}"] = _scalar(value)
    return exported


def flatten_result(result: Mapping[str, Any] | None) -> dict[str, Any]:
    """Converte contratos novos e retornos legados sem colisão de colunas."""
    if not result:
        return {}
    if any(key in result for key in ("outcome", "requested", "confirmed", "person", "margins", "raw")):
        return _flatten_canonical_result(result)
    return {
        f"RETORNO_{_column_fragment(key)}": _scalar(value)
        for key, value in result.items()
    }


def merge_export_columns(
    source: Mapping[str, Any], output: Mapping[str, Any]
) -> dict[str, Any]:
    """Acrescenta a saída sem jamais substituir uma coluna importada.

    Planilhas externas podem conter nomes que o sistema também usa, inclusive
    ``RETORNO_*`` e ``Status_Item``. Nessa situação a entrada mantém o nome e a
    coluna gerada recebe ``SAIDA_`` (e um sufixo numérico, se necessário).
    """
    merged = dict(source)
    for key, value in output.items():
        candidate = str(key)
        if candidate in merged:
            candidate = f"SAIDA_{candidate}"
        suffix = 2
        base = candidate
        while candidate in merged:
            candidate = f"{base}_{suffix}"
            suffix += 1
        merged[candidate] = value
    return merged


def _excel_safe(value: Any) -> Any:
    """Neutraliza fórmulas vindas de planilhas/portais sem alterar números."""
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def _result_rows(session: Session, job_id: int):
    statement = (
        select(JobItem, DatasetRecord, ConsultationResult)
        .join(DatasetRecord, DatasetRecord.id == JobItem.dataset_record_id)
        .outerjoin(ConsultationResult, ConsultationResult.job_item_id == JobItem.id)
        .where(JobItem.job_id == job_id)
        .order_by(JobItem.id)
        .execution_options(yield_per=500)
    )
    result = session.execute(statement)
    if hasattr(result, "yield_per"):
        return result.yield_per(500)
    return iter(result.all())


def _export_row(
    cipher: SecretCipher,
    item: JobItem,
    record: DatasetRecord,
    result: ConsultationResult | None,
) -> dict[str, Any]:
    source = json.loads(
        cipher.decrypt(
            record.source_ciphertext,
            context=f"record:{record.encryption_context}:source",
        )
    )
    result_data: dict[str, Any] = {}
    if (
        result
        and result.superseded_at is None
        and (result.attempt_number is None or result.attempt_number == item.attempts)
    ):
        result_data = json.loads(
            cipher.decrypt(
                result.result_ciphertext,
                context=f"result:{item.id}",
            )
        )
    return merge_export_columns(
        source,
        {
            **flatten_result(result_data),
            "Status_Item": item.status,
            "Resultado_Item": getattr(item, "outcome", None),
            "Tentativas": item.attempts,
            "Codigo_Erro": item.error_code,
            "Mensagem_Erro": item.error_message,
        },
    )


def build_job_export(session: Session, settings: Settings, job_id: int) -> tuple[bytes, int]:
    cipher = SecretCipher(settings.master_key)
    column_order: dict[str, None] = {}
    row_count = 0
    # Primeira passagem: descobre somente o schema dinâmico, sem reter linhas.
    for item, record, result in _result_rows(session, job_id):
        row = _export_row(cipher, item, record, result)
        for column in row:
            column_order.setdefault(column, None)
        row_count += 1

    columns = list(column_order)
    workbook = Workbook(write_only=True)
    worksheet = workbook.create_sheet("Resultados")
    worksheet.append(columns)
    # Segunda passagem: escreve em streaming. O custo é uma nova descriptografia,
    # mas o uso de memória deixa de crescer com o tamanho da base.
    for item, record, result in _result_rows(session, job_id):
        row = _export_row(cipher, item, record, result)
        worksheet.append([_excel_safe(row.get(column)) for column in columns])
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue(), row_count

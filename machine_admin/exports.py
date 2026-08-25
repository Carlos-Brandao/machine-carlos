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
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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


def _margin_fragment(value: object) -> str:
    """Normaliza o tipo sem produzir ``MARGEM_MARGEM_*``."""
    fragment = _column_fragment(value)
    if fragment == "MARGEM":
        return ""
    while fragment.startswith("MARGEM_"):
        fragment = fragment.removeprefix("MARGEM_")
    return fragment


def _margin_column(margin_type: str, detail: object | None = None) -> str:
    column = f"MARGEM_{margin_type}" if margin_type else "MARGEM"
    if detail is not None:
        column += f"_{_column_fragment(detail)}"
    return column


def _semantic_fragment(value: object) -> str:
    """Identifica a informação, independentemente do namespace de saída."""
    fragment = _column_fragment(value)
    for prefix in ("SAIDA_", "RETORNO_", "SERVIDOR_", "CONFIRMADO_", "SOLICITADO_"):
        if fragment.startswith(prefix):
            fragment = fragment.removeprefix(prefix)
            break
    aliases = {
        "CPF_RETORNADO": "CPF",
        "CPF_CONFIRMADO": "CPF",
        "REGISTRATION": "MATRICULA",
        "CARTAO_CONSIGNADO": "MARGEM_CARTAO_CONSIGNADO",
        "SALARIO_BASE": "MARGEM_SALARIO_BASE",
        "MEDIA_MARGEM_12_MESES": "MARGEM_MEDIA_MARGEM_12_MESES",
    }
    fragment = aliases.get(fragment, fragment)
    if fragment.startswith("MARGEM_"):
        detail = fragment
        while detail.startswith("MARGEM_"):
            detail = detail.removeprefix("MARGEM_")
        return f"MARGEM_{detail}"
    return fragment


def _values_equivalent(left: Any, right: Any, semantic: str = "") -> bool:
    """Compara representações equivalentes sem confundir campos distintos."""
    if left is None or right is None:
        return left is right
    left_value = _scalar(left)
    right_value = _scalar(right)
    if semantic == "CPF":
        left_digits = re.sub(r"\D", "", str(left_value))
        right_digits = re.sub(r"\D", "", str(right_value))
        return bool(left_digits) and left_digits == right_digits
    if semantic == "MATRICULA":
        left_registration = re.sub(r"[^A-Z0-9]", "", str(left_value).upper())
        right_registration = re.sub(r"[^A-Z0-9]", "", str(right_value).upper())
        return bool(left_registration) and left_registration == right_registration
    if semantic == "MARGEM" or semantic.startswith("MARGEM_") or "SALARIO" in semantic:
        left_decimal = _decimal_value(left_value)
        right_decimal = _decimal_value(right_value)
        if left_decimal is not None and right_decimal is not None:
            return left_decimal == right_decimal
    return str(left_value).strip() == str(right_value).strip()


def _decimal_value(value: Any) -> Decimal | None:
    text = re.sub(r"[^0-9,.-]", "", str(value).strip())
    if not text:
        return None
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def job_export_filename(
    municipality: str,
    *,
    exported_at: datetime | None = None,
    timezone_name: str = "America/Fortaleza",
) -> str:
    """Nome único e legível para download e envio do resultado final."""
    try:
        timezone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        timezone = ZoneInfo("America/Fortaleza")
    moment = exported_at or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    local = moment.astimezone(timezone)
    normalized = unicodedata.normalize("NFKD", str(municipality or "Convenio"))
    ascii_name = normalized.encode("ascii", "ignore").decode("ascii")
    words = re.findall(r"[A-Za-z0-9]+", ascii_name)[:20]
    friendly = "_".join(
        word if word.isupper() and len(word) <= 4 else word.capitalize()
        for word in words
    )[:120].strip("_") or "Convenio"
    return f"{local:%Y-%m-%d_%H-%M-%S}_{friendly}_MargemConsultada.xlsx"


def _flatten_canonical_result(result: Mapping[str, Any]) -> dict[str, Any]:
    exported: dict[str, Any] = {}
    canonical_values: dict[str, list[Any]] = {}

    def add_canonical(column: str, value: Any) -> None:
        scalar = _scalar(value)
        if scalar in (None, ""):
            return
        exported[column] = scalar
        canonical_values.setdefault(_semantic_fragment(column), []).append(scalar)

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
                add_canonical(f"{prefix}_{_column_fragment(key)}", value)

    margins = result.get("margins")
    if isinstance(margins, Mapping):
        for key, value in margins.items():
            margin_type = _margin_fragment(key)
            if isinstance(value, Mapping):
                for detail_key, detail_value in value.items():
                    add_canonical(
                        _margin_column(margin_type, detail_key),
                        detail_value,
                    )
            else:
                add_canonical(_margin_column(margin_type), value)
    elif isinstance(margins, list):
        for index, margin in enumerate(margins, start=1):
            if not isinstance(margin, Mapping):
                continue
            margin_type = _margin_fragment(margin.get("type") or index)
            for key, value in margin.items():
                if key == "type":
                    continue
                add_canonical(_margin_column(margin_type, key), value)

    raw = result.get("raw")
    if isinstance(raw, Mapping):
        for key, value in raw.items():
            scalar = _scalar(value)
            if scalar in (None, ""):
                continue
            column = f"RETORNO_{_column_fragment(key)}"
            semantic = _semantic_fragment(column)
            if any(
                _values_equivalent(value, canonical, semantic)
                for canonical in canonical_values.get(semantic, ())
            ):
                continue
            exported[column] = scalar

    consumed = {
        "outcome",
        "status",
        "requested",
        "confirmed",
        "person",
        "margins",
        "raw",
    }
    for key, value in result.items():
        if key not in consumed:
            scalar = _scalar(value)
            if scalar in (None, ""):
                continue
            column = f"RETORNO_{_column_fragment(key)}"
            semantic = _semantic_fragment(column)
            if isinstance(raw, Mapping) and key in raw and _values_equivalent(
                value, raw[key], semantic
            ):
                continue
            if any(
                _values_equivalent(value, canonical, semantic)
                for canonical in canonical_values.get(semantic, ())
            ):
                continue
            exported[column] = scalar
    return exported


def flatten_result(result: Mapping[str, Any] | None) -> dict[str, Any]:
    """Converte contratos novos e retornos legados sem colisão de colunas."""
    if not result:
        return {}
    if any(key in result for key in ("outcome", "requested", "confirmed", "person", "margins", "raw")):
        return _flatten_canonical_result(result)
    return {
        f"RETORNO_{_column_fragment(key)}": scalar
        for key, value in result.items()
        if (scalar := _scalar(value)) not in (None, "")
    }


def merge_export_columns(
    source: Mapping[str, Any], output: Mapping[str, Any]
) -> dict[str, Any]:
    """Acrescenta a saída sem jamais substituir uma coluna importada.

    Planilhas externas podem conter nomes que o sistema também usa. Dados de
    domínio idênticos são emitidos uma vez; campos operacionais oficiais nunca
    são confundidos com a entrada e recebem ``SAIDA_`` em caso de colisão.
    """
    merged = dict(source)
    source_values: dict[str, list[Any]] = {}
    for key, value in source.items():
        source_values.setdefault(_semantic_fragment(key), []).append(value)
    for key, value in output.items():
        candidate = str(key)
        semantic = _semantic_fragment(candidate)
        operational = candidate in {
            "Resultado",
            "Status_Item",
            "Resultado_Item",
            "Tentativas",
            "Codigo_Erro",
            "Mensagem_Erro",
        }
        if not operational and any(
            _values_equivalent(value, source_value, semantic)
            for source_value in source_values.get(semantic, ())
        ):
            continue
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

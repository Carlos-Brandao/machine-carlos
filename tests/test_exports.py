from __future__ import annotations

import unittest
from datetime import UTC, datetime

from machine_admin.exports import (
    flatten_result,
    job_export_filename,
    merge_export_columns,
)


class ExportContractTests(unittest.TestCase):
    def test_legacy_fields_are_namespaced(self) -> None:
        exported = flatten_result({"CPF": "123", "Nome Servidor": "Maria"})

        self.assertEqual("123", exported["RETORNO_CPF"])
        self.assertEqual("Maria", exported["RETORNO_NOME_SERVIDOR"])
        self.assertNotIn("CPF", exported)

    def test_canonical_result_has_predictable_columns(self) -> None:
        exported = flatten_result(
            {
                "outcome": "found",
                "confirmed": {"cpf": "123"},
                "person": {"nome": "Maria"},
                "margins": [
                    {"type": "consignável", "available": 125.4, "currency": "BRL"}
                ],
                "raw": {"Campo do portal": "valor"},
            }
        )

        self.assertEqual("found", exported["Resultado"])
        self.assertEqual("123", exported["CONFIRMADO_CPF"])
        self.assertEqual("Maria", exported["SERVIDOR_NOME"])
        self.assertEqual(125.4, exported["MARGEM_CONSIGNAVEL_AVAILABLE"])
        self.assertEqual("valor", exported["RETORNO_CAMPO_DO_PORTAL"])

    def test_adapter_margin_mapping_is_exported_without_becoming_json(self) -> None:
        exported = flatten_result(
            {
                "outcome": "found",
                "margins": {
                    "consignavel": "125,40",
                    "cartao": {"available": "30,00", "currency": "BRL"},
                },
            }
        )

        self.assertEqual("125,40", exported["MARGEM_CONSIGNAVEL"])
        self.assertEqual("30,00", exported["MARGEM_CARTAO_AVAILABLE"])

    def test_canonical_fields_are_not_repeated_from_raw(self) -> None:
        exported = flatten_result(
            {
                "outcome": "found",
                "person": {"Nome": "Maria"},
                "margins": {"Margem Consignável": "500,00"},
                "raw": {
                    "Nome": "Maria",
                    "Margem Consignável": "500,00",
                    "Prazo final vínculo": "2028-12-31",
                },
            }
        )

        self.assertEqual("Maria", exported["SERVIDOR_NOME"])
        self.assertEqual("500,00", exported["MARGEM_CONSIGNAVEL"])
        self.assertNotIn("RETORNO_NOME", exported)
        self.assertNotIn("RETORNO_MARGEM_CONSIGNAVEL", exported)
        self.assertNotIn("MARGEM_MARGEM_CONSIGNAVEL", exported)
        self.assertEqual(
            "2028-12-31", exported["RETORNO_PRAZO_FINAL_VINCULO"]
        )

    def test_rf1_special_margin_keys_are_not_repeated_from_raw(self) -> None:
        raw = {
            "Margem_Consignavel": "500,00",
            "Media_Margem_12_Meses": "450,00",
            "Salario_Base": "3.000,00",
            "Cartao_Consignado": "200,00",
        }
        exported = flatten_result(
            {
                "outcome": "found",
                "margins": dict(raw),
                "raw": raw,
            }
        )

        self.assertEqual("500,00", exported["MARGEM_CONSIGNAVEL"])
        self.assertEqual(
            "450,00", exported["MARGEM_MEDIA_MARGEM_12_MESES"]
        )
        self.assertEqual("3.000,00", exported["MARGEM_SALARIO_BASE"])
        self.assertEqual("200,00", exported["MARGEM_CARTAO_CONSIGNADO"])
        self.assertFalse(any(key.startswith("RETORNO_") for key in exported))

    def test_raw_difference_is_preserved_for_audit(self) -> None:
        exported = flatten_result(
            {
                "outcome": "found",
                "margins": {"Margem Consignável": "500"},
                "raw": {"Margem Consignável": "450"},
            }
        )

        self.assertEqual("500", exported["MARGEM_CONSIGNAVEL"])
        self.assertEqual("450", exported["RETORNO_MARGEM_CONSIGNAVEL"])

    def test_equal_source_and_output_are_not_exported_twice(self) -> None:
        merged = merge_export_columns(
            {"CPF": "529.982.247-25", "MARGEM_CONSIGNAVEL": 500.0},
            {
                "SOLICITADO_CPF": "52998224725",
                "MARGEM_CONSIGNAVEL": "R$ 500,00",
            },
        )

        self.assertEqual(
            {"CPF": "529.982.247-25", "MARGEM_CONSIGNAVEL": 500.0}, merged
        )

    def test_requested_identifiers_are_not_repeated_in_canonical_export(self) -> None:
        exported = flatten_result(
            {
                "outcome": "found",
                "requested": {"cpf": "52998224725", "registration": "AB-01"},
                "confirmed": {"cpf": "52998224725", "registration": "AB-01"},
            }
        )

        self.assertNotIn("SOLICITADO_CPF", exported)
        self.assertNotIn("SOLICITADO_REGISTRATION", exported)
        self.assertEqual("52998224725", exported["CONFIRMADO_CPF"])
        self.assertEqual("AB-01", exported["CONFIRMADO_REGISTRATION"])

    def test_different_source_and_output_are_both_preserved(self) -> None:
        merged = merge_export_columns(
            {"MARGEM_CONSIGNAVEL": 450}, {"MARGEM_CONSIGNAVEL": 500}
        )

        self.assertEqual(450, merged["MARGEM_CONSIGNAVEL"])
        self.assertEqual(500, merged["SAIDA_MARGEM_CONSIGNAVEL"])

    def test_operational_columns_keep_system_provenance_on_equal_values(self) -> None:
        merged = merge_export_columns(
            {"Status_Item": "completed"}, {"Status_Item": "completed"}
        )

        self.assertEqual("completed", merged["Status_Item"])
        self.assertEqual("completed", merged["SAIDA_Status_Item"])

    def test_export_filename_uses_local_time_and_friendly_agreement(self) -> None:
        filename = job_export_filename(
            "Boa Vista / RR",
            exported_at=datetime(2026, 8, 26, 2, 30, tzinfo=UTC),
            timezone_name="America/Fortaleza",
        )

        self.assertEqual(
            "2026-08-25_23-30-00_Boa_Vista_RR_MargemConsultada.xlsx",
            filename,
        )


if __name__ == "__main__":
    unittest.main()

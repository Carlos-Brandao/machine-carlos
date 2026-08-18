from __future__ import annotations

import unittest

from machine_admin.exports import flatten_result


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


if __name__ == "__main__":
    unittest.main()

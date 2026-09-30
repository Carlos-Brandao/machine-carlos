"""Regression tests for service boundaries that do not need external services."""

from __future__ import annotations

import unittest
from unittest.mock import patch


from services.database import require_postgres_url
from services.registry import (
    MUNICIPALITIES,
    PLATFORMS,
    enabled_municipalities,
    runner_names,
)
from services.scheduling import is_within_window, platform_for
from services.utils import mask_cpf


class ServiceTests(unittest.TestCase):
    def test_registry_is_the_single_consistent_source(self) -> None:
        self.assertNotIn("fenix", runner_names())
        self.assertIn("consiglog", runner_names())
        self.assertEqual("safeconsig", platform_for("maranguape"))
        for municipality in enabled_municipalities():
            self.assertIn(municipality.slug, MUNICIPALITIES)
            self.assertTrue(PLATFORMS[municipality.platform_slug].enabled)
            # Toda plataforma ativa precisa ser aceita pela política de horário.
            self.assertIsInstance(is_within_window(municipality.platform_slug), bool)

    def test_cpf_is_masked_in_logs(self) -> None:
        masked = mask_cpf("028.851.452-18")
        self.assertEqual("***.***.***-5218", masked)
        self.assertNotIn("02885145218", masked)

    def test_runtime_requires_postgresql(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(RuntimeError):
                require_postgres_url()
        with patch.dict(
            "os.environ",
            {"DATABASE_URL": "postgresql://machine:secret@localhost/machine"},
            clear=True,
        ):
            self.assertTrue(require_postgres_url().startswith("postgresql+psycopg://"))

if __name__ == "__main__":
    unittest.main()

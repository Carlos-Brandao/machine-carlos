from __future__ import annotations
import io
import json
import os
import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch
from openpyxl import load_workbook
from machine_admin.product_exports import render_snapshot
from machine_admin.scheduling import next_occurrence, update_schedule
from machine_admin.webhooks import validate_webhook_url


class ProductOperationsTests(unittest.TestCase):
    def test_cron_uses_agreement_timezone_and_supports_lists_ranges_steps(self):
        self.assertEqual(datetime(2026, 10, 1, 11, 0, tzinfo=UTC), next_occurrence("0,30 8-10/2 * * 1-5", "America/Fortaleza", datetime(2026, 9, 30, 14, 0, tzinfo=UTC)))

    def test_cron_skips_nonexistent_dst_and_fires_fall_fold_only_once(self):
        self.assertEqual(datetime(2026, 3, 9, 6, 30, tzinfo=UTC), next_occurrence("30 2 * * *", "America/New_York", datetime(2026, 3, 8, 0, 0, tzinfo=UTC)))
        first = next_occurrence("30 1 * * *", "America/New_York", datetime(2026, 11, 1, 0, 0, tzinfo=UTC))
        self.assertEqual(datetime(2026, 11, 1, 5, 30, tzinfo=UTC), first)
        self.assertEqual(datetime(2026, 11, 2, 6, 30, tzinfo=UTC), next_occurrence("30 1 * * *", "America/New_York", first))

    def test_cron_rejects_invalid_expression_and_impossible_date(self):
        for expression in ("60 * * * *", "* * *", "*/0 * * * *", "0 0 31 2 *"):
            with self.assertRaises(ValueError):
                next_occurrence(expression, "UTC", datetime(2026, 1, 1, tzinfo=UTC))

    def test_broken_agreement_can_always_have_schedule_disabled(self):
        schedule = SimpleNamespace(enabled=True)
        session = Mock()
        with patch("machine_admin.scheduling.validate_execution_selection", side_effect=AssertionError("must not validate a disabled schedule")):
            update_schedule(session, schedule, enabled=False)
        self.assertFalse(schedule.enabled)

    def test_all_export_formats_keep_rows_and_neutralize_spreadsheet_formulas(self):
        rows = [{"CPF": "00123456797", "Margem": 0, "=HEADER": "=HYPERLINK(\"bad\")"}]
        payload = render_snapshot(rows, "xlsx")
        values = list(load_workbook(io.BytesIO(payload)).active.values)
        self.assertEqual(("CPF", "Margem", "'=HEADER"), values[0])
        self.assertEqual("00123456797", values[1][0])
        self.assertEqual(0, values[1][1])
        self.assertTrue(values[1][2].startswith("'="))
        self.assertIn("'=HEADER", render_snapshot(rows, "csv").decode("utf-8-sig"))
        self.assertEqual(rows, json.loads(render_snapshot(rows, "json")))

    def test_webhooks_require_explicit_https_destination_allowlist(self):
        with patch.dict(os.environ, {"WEBHOOK_ALLOWED_HOSTS": "api.example.test"}):
            self.assertEqual("https://api.example.test/hook", validate_webhook_url("https://api.example.test/hook"))
            for url in ("https://other.test/hook", "http://api.example.test", "https://x:y@api.example.test/hook", "https://api.example.test:444/hook"):
                with self.assertRaises(ValueError):
                    validate_webhook_url(url)

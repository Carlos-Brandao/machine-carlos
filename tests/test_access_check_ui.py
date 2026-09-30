"""Presentation contracts for explicit, cancellable access checks."""
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase

from machine_admin.product_admin import TEMPLATES


class AccessCheckPresentationTests(TestCase):
    def render_panel(self, status=None, *, can_manage=True, message="Aguardando executor."):
        credential = SimpleNamespace(id=3, status="active")
        checks = [] if status is None else [SimpleNamespace(
            id=8, status=status, message=message, error_code=None,
            created_at=datetime(2026, 9, 30, 12, 0, tzinfo=UTC))]
        return str(TEMPLATES.env.get_template("_access_check.html").module.access_check_panel(
            credential, checks, "csrf-test", can_manage=can_manage))

    def test_active_check_can_be_cancelled_and_not_repeated(self):
        for status in ("queued", "running"):
            html = self.render_panel(status)
            self.assertIn('data-access-check-start-button disabled', html)
            self.assertIn('/admin/credentials/3/tests/8/cancel', html)
            self.assertNotIn('data-access-check-cancel hidden', html)
            self.assertIn('name="csrf" value="csrf-test"', html)

    def test_cancelling_has_explanation_and_no_second_cancel(self):
        html = self.render_panel("cancelling")
        self.assertIn("Cancelando teste", html)
        self.assertIn("encerrar a sessão do portal com segurança", html)
        self.assertIn('data-access-check-cancel hidden', html)
        self.assertIn('data-access-check-start-button disabled', html)

    def test_success_is_not_shown_as_failure_and_diagnostics_are_escaped(self):
        html = self.render_panel("success", message='<img src=x onerror="bad()">')
        self.assertIn("Login confirmado", html)
        self.assertNotIn('<img src=x', html)
        self.assertIn('&lt;img', html)
        self.assertIn('data-access-check-cancel hidden', html)

    def test_viewer_has_no_control_or_polling(self):
        html = self.render_panel("running", can_manage=False)
        self.assertNotIn('data-access-check-monitor', html)
        self.assertNotIn('<form', html)
        self.assertIn("Testando login", html)

    def test_both_credential_screens_share_check_controls(self):
        for template in ("credential_edit.html", "credentials.html"):
            source = (Path(__file__).resolve().parents[1] / "machine_admin/templates" / template).read_text()
            self.assertIn('import access_check_panel', source)
            self.assertIn('access_check_panel(', source)

    def test_list_and_detail_expose_pause_for_blocked_queued_work(self):
        folder = Path(__file__).resolve().parents[1] / "machine_admin/templates"
        for name in ("jobs.html", "consultation_detail.html"):
            source = (folder / name).read_text()
            self.assertIn("'queued','running','blocked','awaiting_dataset'", source)
        self.assertIn('data-job-list-pause', (folder / "jobs.html").read_text())
        self.assertIn('data-job-draining', (folder / "consultation_detail.html").read_text())

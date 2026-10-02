"""Changing agreement identity rules cannot invalidate populated typed catalogs."""

import base64
import json
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner

from machine_admin.config import Settings
from machine_admin.db import get_db
from machine_admin.models import AdminUser, Municipality
from machine_admin.web import create_app


class DatasetRuleGuardTests(TestCase):
    def setUp(self):
        self.settings = Settings(
            "postgresql://unused:unused@localhost/unused", "s" * 48, b"k" * 32,
            False, ("testserver",), Path("/unused"), 1024, None, None,
        )
        self.municipality = Municipality(
            slug="test", name="Test", platform_slug="rf1", enabled=True,
            input_schema={"required": ["cpf"], "optional": ["registration"], "deduplication_key": ["cpf"]},
        )
        user = AdminUser(id=1, email="admin@example.invalid", display_name="QA", role="admin", active=True, session_version=1)
        self.typed_dataset_id = 3
        self.calls = []
        self.guard_query = None
        self.policy_after_refresh = None
        case = self

        class Session:
            def get(self, model, key):
                return {AdminUser: user, Municipality: case.municipality}.get(model)

            def refresh(self, value):
                case.calls.append("refresh")
                if case.policy_after_refresh is not None:
                    value.input_schema = case.policy_after_refresh

            def scalar(self, statement):
                case.calls.append("find_typed")
                case.guard_query = statement
                return case.typed_dataset_id

            def add(self, value): pass
            def flush(self): pass
            def commit(self): case.calls.append("commit")
            def rollback(self): case.calls.append("rollback")

        app = create_app(self.settings)
        app.dependency_overrides[get_db] = Session
        self.client = TestClient(app)
        self.signer = TimestampSigner(self.settings.session_secret)
        cookie = self.signer.sign(base64.b64encode(json.dumps({
            "user_id": 1, "session_version": 1, "csrf": "valid",
        }).encode())).decode()
        self.client.cookies.set("machine_admin_session", cookie, domain="testserver.local", path="/")

    def submit(self, require_registration=True):
        data = {
            "csrf": "valid", "name": "Test", "operational_status": "draft",
            "timezone": "America/Fortaleza", "max_workers": "1", "weekdays": "1", "enabled": "on",
        }
        if require_registration:
            data["registration_required"] = "on"
        with patch("machine_admin.web.lock_dataset_catalog", side_effect=lambda *args: self.calls.append("lock")):
            response = self.client.post("/admin/agreements/test", data=data, follow_redirects=False)
        self.assertEqual(303, response.status_code, response.text)
        session = json.loads(base64.b64decode(self.signer.unsign(self.client.cookies.get("machine_admin_session"))))
        return session["flash"]

    def test_typed_bases_block_identity_change_under_catalog_lock(self):
        flash = self.submit()
        self.assertEqual("error", flash["level"])
        self.assertIn("bases classificadas ativas", flash["message"])
        self.assertEqual(["cpf"], self.municipality.input_schema["deduplication_key"])
        self.assertEqual(["lock", "refresh", "find_typed", "rollback"], self.calls)
        query = str(self.guard_query)
        self.assertIn("datasets.dataset_type IS NOT NULL", query)
        self.assertIn("datasets.status !=", query)

    def test_legacy_or_empty_catalog_can_change_rules_before_classification(self):
        self.typed_dataset_id = None
        flash = self.submit()
        self.assertEqual("success", flash["level"])
        self.assertEqual(["cpf", "registration"], self.municipality.input_schema["deduplication_key"])

    def test_same_rules_remain_editable_and_stale_schema_is_refreshed(self):
        self.policy_after_refresh = {"deduplication_key": ["cpf", "registration"]}
        flash = self.submit()
        self.assertEqual("success", flash["level"])
        self.assertNotIn("find_typed", self.calls)

"""Credential forms derive the processor from the agreement only."""

import base64
import json
from pathlib import Path
from unittest import TestCase

from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner

from machine_admin.config import Settings
from machine_admin.db import get_db
from machine_admin.models import AdminUser, Municipality, Platform, PortalCredential
from machine_admin.web import create_app


class CredentialFieldsTests(TestCase):
    def setUp(self):
        settings = Settings(
            database_url="postgresql://unused:unused@localhost/unused",
            session_secret="s" * 48, master_key=b"k" * 32,
            cookie_secure=False, allowed_hosts=("testserver",),
            storage_dir=Path("/unused"), max_upload_bytes=1024,
            bootstrap_admin_email=None, bootstrap_admin_password=None,
        )
        user = AdminUser(id=1, email="admin@example.invalid", display_name="QA",
                         role="admin", active=True, session_version=1)
        platform = Platform(slug="rf1", name="RF1", runner="rf1", enabled=True)
        municipality = Municipality(slug="test", name="Test", platform=platform,
                                    platform_slug="rf1", enabled=True)
        self.credential = PortalCredential(
            id=3, municipality_slug="test", label="Principal", status="active",
            portal_username="operador", portal_password="senha-antiga",
            encryption_context="test-context", failure_count=0,
            portal_profile="Portal option 42", consignataria="Portal option 42",
            settings_json={"portal_profile": "Portal option 42", "consignataria": "Portal option 42"},
        )
        case = self
        self.added = []

        class Session:
            def get(self, model, key):
                return {AdminUser: user, Municipality: municipality,
                        PortalCredential: case.credential}.get(model)

            def scalar(self, statement):
                return None

            def scalars(self, statement):
                model = statement.column_descriptions[0].get("entity")
                return {Municipality: [municipality], PortalCredential: [case.credential]}.get(model, [])

            def add(self, value):
                case.added.append(value)

            def refresh(self, value, **kwargs): pass
            def flush(self): pass
            def commit(self): pass
            def rollback(self): pass

        app = create_app(settings)
        app.dependency_overrides[get_db] = Session
        self.client = TestClient(app)
        cookie = TimestampSigner(settings.session_secret).sign(base64.b64encode(
            json.dumps({"user_id": 1, "session_version": 1, "csrf": "valid"}).encode()
        )).decode()
        self.client.cookies.set("machine_admin_session", cookie)

    def test_edit_changes_password_and_retains_internal_portal_selection(self):
        response = self.client.post("/admin/credentials/3/edit", data={
            "label": "Principal", "username": "operador", "password": "senha-corrigida",
            "csrf": "valid",
        }, follow_redirects=False)
        self.assertEqual(303, response.status_code, response.text)
        self.assertEqual("senha-corrigida", self.credential.portal_password)
        self.assertEqual("Portal option 42", self.credential.portal_profile)
        self.assertEqual("Portal option 42", self.credential.consignataria)
        self.assertEqual("Portal option 42", self.credential.settings_json["portal_profile"])

    def test_create_needs_only_agreement_label_and_login(self):
        response = self.client.post("/admin/credentials", data={
            "municipality_slug": "test", "label": "Segundo", "username": "operador2",
            "password": "senha-nova", "csrf": "valid",
        }, follow_redirects=False)
        self.assertEqual(303, response.status_code, response.text)
        added = next(value for value in self.added if isinstance(value, PortalCredential))
        self.assertEqual("test", added.municipality_slug)
        self.assertIsNone(added.consignataria)
        self.assertIsNone(added.portal_profile)

    def test_listing_and_edit_keep_processor_without_the_removed_field(self):
        for path in ("/admin/credentials", "/admin/credentials/3/edit"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(200, response.status_code, response.text)
                self.assertIn("RF1", response.text)
                self.assertNotIn('name="consignataria"', response.text)
                self.assertNotIn("Portal option 42", response.text)

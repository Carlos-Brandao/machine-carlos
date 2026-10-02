"""HTTP regressions for typed complements, permissions, and safe rollback."""
import base64
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from datetime import UTC, datetime
from unittest.mock import patch

from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner

from machine_admin.config import Settings
from machine_admin.db import get_db
from machine_admin.models import AdminUser, ApiToken, Dataset, Municipality, Platform
from machine_admin.web import create_app


class DatasetAdminTests(unittest.TestCase):
    def setUp(self):
        now = datetime.now(UTC)
        self.settings = Settings(database_url="postgresql://unused:unused@localhost/unused", session_secret="s"*48, master_key=b"k"*32, cookie_secure=False, allowed_hosts=("testserver",), storage_dir=Path("/unused"), max_upload_bytes=1024, bootstrap_admin_email=None, bootstrap_admin_password=None)
        self.user = AdminUser(id=1, email="qa@example.invalid", display_name="QA", role="admin", active=True, session_version=1)
        self.token = ApiToken(id=1, owner_id=1, name="QA", scopes=["datasets:read", "datasets:write"], last_used_at=now)
        self.municipality = Municipality(slug="test", name="Test", platform_slug="rf1", enabled=True, input_schema={})
        self.dataset = Dataset(id=2, municipality_slug="test", uploaded_by_id=1, display_name="Base original", original_filename="qa.csv", dataset_type="efetivos", row_count=4, status="ready", created_at=now, updated_at=now, metadata_json={"added_row_count": 1, "existing_row_count": 3, "general_added_row_count": 1, "duplicate_row_count": 0, "invalid_row_count": 2, "import_action": "complemented"})
        self.destinations = [self.dataset]
        self.commit_error = None
        case = self

        class Session:
            def get(self, model, key):
                return {AdminUser: case.user, ApiToken: case.token, Municipality: case.municipality, Dataset: case.dataset}.get(model)

            def scalar(self, statement):
                entity = statement.column_descriptions[0].get("entity")
                if entity is ApiToken:
                    return case.token
                if entity is Dataset:
                    params = statement.compile().params
                    row = next((item for item in case.destinations if item.id == params.get("id_1")), None)
                    if row and ("uploaded_by_id_1" not in params or row.uploaded_by_id == params["uploaded_by_id_1"]):
                        return row
                return None

            def scalars(self, statement):
                entity = statement.column_descriptions[0].get("entity")
                return {Dataset: case.destinations, Municipality: [case.municipality]}.get(entity, [])

            def add(self, item): pass
            def execute(self, statement): return []
            def rollback(self): pass
            def commit(self):
                if case.commit_error:
                    raise case.commit_error

        self.session = Session()
        app = create_app(self.settings)
        app.dependency_overrides[get_db] = lambda: self.session
        self.client = TestClient(app)
        cookie = TimestampSigner(self.settings.session_secret).sign(base64.b64encode(json.dumps({"user_id": 1, "session_version": 1, "csrf": "valid"}).encode())).decode()
        self.client.cookies.set("machine_admin_session", cookie)
        self.headers = {"Authorization": "Bearer synthetic-test-token"}
        self.form = {"municipality_slug": "test", "display_name": "Segundo arquivo", "dataset_type": "efetivos", "csrf": "valid"}
        self.files = {"file": ("qa.csv", b"CPF\n52998224725\n", "text/csv")}

    def test_upload_requires_name_type_and_reports_complement(self):
        with patch("machine_admin.product_api.import_dataset", return_value=self.dataset) as importer:
            response = self.client.post("/api/v1/datasets", headers=self.headers, data=self.form, files=self.files)
        self.assertEqual(201, response.status_code, response.text)
        body = response.json()
        self.assertEqual("Base original", body["name"])
        self.assertEqual("efetivos", body["dataset_type"])
        self.assertEqual(1, body["import"]["added_row_count"])
        self.assertEqual(3, body["import"]["existing_row_count"])
        self.assertEqual(2, body["import"]["invalid_row_count"])
        self.assertEqual("efetivos", importer.call_args.kwargs["dataset_type"])
        self.assertNotIn("duplicate_policy", importer.call_args.kwargs)
        for field in ("display_name", "dataset_type"):
            response = self.client.post("/api/v1/datasets", headers=self.headers, data={k:v for k,v in self.form.items() if k != field}, files=self.files)
            self.assertEqual(422, response.status_code)

    def test_operator_cannot_complement_foreign_general_and_locks_first(self):
        self.user.role = "operator"
        self.destinations.append(Dataset(id=3, municipality_slug="test", uploaded_by_id=2, dataset_type="geral", status="ready"))
        events = []
        original_scalars = self.session.scalars
        self.session.scalars = lambda query: events.append("ownership_targets") or original_scalars(query)
        with patch("machine_admin.product_api.lock_dataset_catalog", side_effect=lambda *args: events.append("lock")), patch("machine_admin.product_api.import_dataset") as importer:
            response = self.client.post("/api/v1/datasets", headers=self.headers, data=self.form, files=self.files)
        self.assertEqual(404, response.status_code, response.text)
        self.assertEqual(["lock", "ownership_targets"], events)
        importer.assert_not_called()

    def test_read_token_cannot_edit_or_remove(self):
        self.token.scopes = ["datasets:read"]
        with patch("machine_admin.product_api.update_dataset") as update, patch("machine_admin.product_api.archive_dataset") as archive:
            self.assertEqual(403, self.client.patch("/api/v1/datasets/2", headers=self.headers, json={"display_name": "Changed"}).status_code)
            self.assertEqual(403, self.client.delete("/api/v1/datasets/2", headers=self.headers).status_code)
        update.assert_not_called()
        archive.assert_not_called()

    def test_api_edit_conflict_and_logical_removal(self):
        with patch("machine_admin.product_api.update_dataset", side_effect=ValueError("Já existe uma base Geral")):
            response = self.client.patch("/api/v1/datasets/2", headers=self.headers, json={"dataset_type": "geral"})
        self.assertEqual(422, response.status_code)
        self.assertIn("Já existe", response.json()["detail"])
        with patch("machine_admin.product_api.update_dataset", return_value=self.dataset) as update:
            response = self.client.patch("/api/v1/datasets/2", headers=self.headers, json={"display_name": "Novo nome"})
        self.assertEqual(200, response.status_code)
        self.assertEqual("Novo nome", update.call_args.kwargs["display_name"])
        self.assertIsNone(update.call_args.kwargs["dataset_type"])

        def archive(session, *, dataset):
            dataset.status = "archived"
        with patch("machine_admin.product_api.archive_dataset", side_effect=archive):
            response = self.client.delete("/api/v1/datasets/2", headers=self.headers)
        self.assertEqual(200, response.status_code)
        self.assertEqual("archived", response.json()["status"])
        self.assertEqual(4, response.json()["row_count"])

    def test_viewer_and_bad_csrf_cannot_mutate_panel(self):
        with patch("machine_admin.web.update_dataset") as update, patch("machine_admin.web.archive_dataset") as archive:
            self.assertEqual(403, self.client.post("/admin/datasets/2/edit", data={**self.form, "csrf": "bad"}).status_code)
            self.user.role = "viewer"
            self.assertEqual(403, self.client.post("/admin/datasets/2/edit", data=self.form).status_code)
            self.assertEqual(403, self.client.post("/admin/datasets/2/remove", data={"csrf": "valid"}).status_code)
        update.assert_not_called()
        archive.assert_not_called()

    def test_typed_forms_legacy_classification_and_detail_pagination(self):
        response = self.client.get("/admin/datasets")
        self.assertEqual(200, response.status_code, response.text)
        self.assertIn('name="dataset_type" required', response.text)
        self.assertNotIn('name="duplicate_policy"', response.text)
        self.assertNotIn('/admin/consultations/new?dataset_id=', response.text)
        self.assertIn('href="/admin/datasets/2">Ver / editar</a>', response.text)
        self.assertIn('action="/admin/datasets/2/remove"', response.text)
        self.assertIn('>Remover</button>', response.text)
        self.dataset.dataset_type = None
        response = self.client.get("/admin/datasets")
        self.assertEqual(200, response.status_code)
        self.assertIn("classificá-las antes de enviar complementos", response.text)
        page = {"items": [], "page": 2, "limit": 50, "total": 70, "has_next": False}
        with patch("machine_admin.web.dataset_record_page", return_value=page) as loader:
            response = self.client.get("/admin/datasets/2?page=2")
        self.assertEqual(200, response.status_code, response.text)
        self.assertIn("Não classificada", response.text)
        self.assertIn("Selecione para classificar", response.text)
        self.assertIn("?page=1", response.text)
        self.assertIn('href="/admin/consultations/new?dataset_id=2">Iniciar consulta</a>', response.text)
        self.assertEqual("no-store", response.headers["cache-control"])
        self.assertEqual(2, loader.call_args.kwargs["page"])

    def test_failed_commit_only_cleans_new_blob(self):
        with TemporaryDirectory() as directory:
            original = Path(directory) / "committed.enc"
            pending = Path(directory) / "new.enc"
            original.touch()
            pending.touch()
            self.dataset.storage_path = str(original)
            self.dataset._import_blob_path = str(pending)
            self.commit_error = ValueError("Rejected transaction")
            with patch("machine_admin.product_api.import_dataset", return_value=self.dataset):
                response = self.client.post("/api/v1/datasets", headers=self.headers, data=self.form, files=self.files)
            self.assertEqual(422, response.status_code)
            self.assertTrue(original.exists())
            self.assertFalse(pending.exists())


if __name__ == "__main__":
    unittest.main()

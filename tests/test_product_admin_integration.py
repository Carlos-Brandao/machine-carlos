"""Exercise product routes through the real cookie and bearer dependencies."""
import base64
import json
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner

from machine_admin.config import Settings
from machine_admin.db import get_db
from machine_admin.models import AdminUser, ApiToken, CredentialLease, Dataset, ExportArtifact, Job, JobEvent, Municipality, Platform, PortalCredential, Schedule, ScheduleOccurrence
from machine_admin.readiness import ReadinessIssue, ReadinessReport
from machine_admin.web import create_app


class ProductIntegration(unittest.TestCase):
    def setUp(self):
        now = datetime.now(UTC)
        self.settings = Settings(database_url="postgresql://unused:unused@localhost/unused", session_secret="s"*48, master_key=b"k"*32, cookie_secure=False, allowed_hosts=("testserver",), storage_dir=Path("/unused"), max_upload_bytes=1024, bootstrap_admin_email=None, bootstrap_admin_password=None)
        self.user = AdminUser(id=1, email="qa@example.invalid", display_name="QA", role="admin", active=True, session_version=1)
        self.token = ApiToken(id=1, owner_id=1, name="Read-only QA", scopes=["jobs:read", "results:read", "exports:read", "schedules:read"], last_used_at=now)
        self.platform = Platform(slug="rf1", name="RF1", runner="rf1", enabled=True, start_hour=0, end_hour=24)
        self.municipality = Municipality(slug="test", name="Test", platform_slug="rf1", max_workers=2, timezone="America/Fortaleza", schedule_policy={"weekdays":list(range(7))}, enabled=True, operational_status="ready")
        self.dataset = Dataset(id=2, municipality_slug="test", display_name="QA dataset", original_filename="qa.xlsx", row_count=12, status="ready", created_at=now)
        self.credential = PortalCredential(id=3, municipality_slug="test", label="QA access", status="active")
        self.job = Job(id=4, municipality_slug="test", dataset_id=2, status="queued", requested_by_id=1, selected_credential_ids=[3], max_parallel_accounts=1, total_items=12, completed_items=0, failed_items=0, found_items=0, not_found_items=0, retryable_items=0, permanent_items=0, created_at=now)
        self.schedule = Schedule(id=5, name="Daily QA", dataset_id=2, requested_by_id=1, cron_expression="0 8 * * *", timezone="America/Fortaleza", selected_credential_ids=[3], max_parallel_accounts=1, enabled=True, misfire_grace_seconds=300, next_run_at=now+timedelta(days=1))
        self.occurrence = ScheduleOccurrence(id=6, schedule_id=5, scheduled_for=now, status="skipped_overlap", job_id=4, message="Anterior ativa")
        self.artifact = ExportArtifact(id=7, job_id=4, result_version=1, format="csv", status="ready", filename="qa.csv", sha256="a"*64, row_count=12, partial=True, size_bytes=3, created_at=now, ready_at=now)
        case = self
        class FakeSession:
            def get(self, model, key):
                return {AdminUser:case.user,ApiToken:case.token,Platform:case.platform,Municipality:case.municipality,Dataset:case.dataset,Job:case.job,ExportArtifact:case.artifact}.get(model)
            def scalar(self, statement):
                desc = statement.column_descriptions[0]
                if desc["name"] == "id": return None
                return {Job:case.job,ApiToken:case.token,Schedule:case.schedule,ExportArtifact:case.artifact}.get(desc.get("entity"))
            def scalars(self, statement):
                entity = statement.column_descriptions[0].get("entity")
                return {Dataset:[case.dataset],Municipality:[case.municipality],PortalCredential:[case.credential],Schedule:[case.schedule],ScheduleOccurrence:[case.occurrence],ExportArtifact:[case.artifact]}.get(entity, [])
            def execute(self, statement): return []
            def commit(self): pass
            def rollback(self): pass
            def flush(self): pass
        self.app=create_app(self.settings)
        self.app.dependency_overrides[get_db]=lambda:FakeSession()
        self.client=TestClient(self.app)
        cookie = TimestampSigner(self.settings.session_secret).sign(base64.b64encode(json.dumps({"user_id":1,"session_version":1,"csrf":"valid"}).encode())).decode()
        self.client.cookies.set("machine_admin_session",cookie)

    def test_real_cookie_pages_and_block_reason(self):
        for path in ("/admin/consultations/new?dataset_id=2", "/admin/schedules", "/admin/settings"):
            response=self.client.get(path)
            self.assertEqual(200,response.status_code,response.text)
        readiness=ReadinessReport("test","ready",False,(ReadinessIssue("worker_offline","Serviço offline.","Verifique o serviço."),),1,0)
        with patch("machine_admin.web.assess_municipality",return_value=readiness):
            response=self.client.get("/admin/consultations/4/status")
            detail=self.client.get("/admin/consultations/4")
        self.assertEqual(200,response.status_code,response.text)
        self.assertFalse(response.json()["execution"]["executable"])
        self.assertEqual("Serviço offline.",response.json()["execution"]["reason"])
        self.assertEqual(0,response.json()["capacity"]["effective_limit"])
        self.assertEqual(200,detail.status_code,detail.text)

    def test_schedule_history_and_account_labels_render(self):
        response=self.client.get("/admin/schedules")
        self.assertIn("QA dataset",response.text)
        self.assertIn("QA access",response.text)
        self.assertIn("Consulta anterior ainda ativa",response.text)
        self.assertIn("Pausar agenda",response.text)

    def test_bearer_read_cannot_generate_export_but_can_download(self):
        headers={"Authorization":"Bearer synthetic-test-token"}
        response=self.client.post("/api/v1/jobs/4/exports",json={"format":"csv"},headers=headers)
        self.assertEqual(403,response.status_code)
        with patch("machine_admin.product_api.read_export",return_value=b"CPF"):
            response=self.client.get("/api/v1/exports/7/download",headers=headers)
        self.assertEqual(200,response.status_code,response.text)
        self.assertEqual(b"CPF",response.content)
        self.assertEqual("no-store",response.headers["cache-control"])

    def test_export_creation_requires_explicit_write_scope(self):
        self.token.scopes.append("exports:write")
        with patch("machine_admin.product_api.request_export",return_value=self.artifact):
            response=self.client.post("/api/v1/jobs/4/exports",json={"format":"csv"},headers={"Authorization":"Bearer synthetic-test-token"})
        self.assertEqual(202,response.status_code,response.text)
        self.assertEqual("/api/v1/exports/7/download",response.json()["download_url"])

    def test_invalid_schedule_csrf_is_rejected_by_real_auth(self):
        response=self.client.post("/admin/schedules/5/toggle",data={"csrf":"invalid"})
        self.assertEqual(403,response.status_code)

    def test_export_form_reuses_artifact_and_is_not_captured_as_control(self):
        with patch("machine_admin.product_exports.request_export",return_value=self.artifact) as create:
            response=self.client.post("/admin/consultations/4/exports",data={"format":"csv","csrf":"valid"},follow_redirects=False)
        self.assertEqual(303,response.status_code,response.text)
        self.assertEqual("/admin/consultations/4#exports",response.headers["location"])
        self.assertEqual("csv",create.call_args.kwargs["format"])
        with patch("machine_admin.product_exports.read_export",return_value=b"CPF"):
            response=self.client.get("/admin/exports/7/download")
        self.assertEqual(200,response.status_code,response.text)
        self.assertEqual(b"CPF",response.content)
        self.assertEqual('"'+'a'*64+'"',response.headers["etag"])

    def test_export_admin_mutations_require_csrf_and_write_role(self):
        response=self.client.post("/admin/consultations/4/exports",data={"format":"csv","csrf":"bad"})
        self.assertEqual(403,response.status_code)
        self.user.role="viewer"
        self.assertEqual(403,self.client.get("/admin/exports/7/download").status_code)
        self.assertEqual(403,self.client.post("/admin/consultations/4/exports",data={"format":"csv","csrf":"valid"}).status_code)


if __name__=="__main__": unittest.main()

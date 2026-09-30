"""Product acceptance in an explicitly isolated PostgreSQL; no live portal calls."""
from __future__ import annotations
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from machine_admin.config import Settings
from machine_admin.db import get_db
from machine_admin.models import AdminUser, ApiToken, Base, ConsultationResult, Dataset, DatasetRecord, ExportArtifact, Job, JobItem, Municipality, NotificationOutbox, Platform, PortalCredential, Schedule, ScheduleOccurrence
from machine_admin.operations import create_execution
from machine_admin.product_api import install_product_routes
from machine_admin.product_exports import process_one_export, read_export, request_export
from machine_admin.scheduling import create_schedule, process_due_schedules
from machine_admin.security import SecretCipher
from machine_admin.webhooks import create_webhook, enqueue_webhooks, process_one_webhook


@unittest.skipUnless(os.getenv("MACHINE_TEST_DATABASE_URL"), "isolated PostgreSQL URL not configured")
class ProductPostgresAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        url = make_url(os.environ["MACHINE_TEST_DATABASE_URL"])
        if url.get_backend_name() != "postgresql" or not (url.database or "").startswith("machine_acceptance_"):
            raise RuntimeError("Refusing non-isolated PostgreSQL database")
        cls.schema = "product_" + secrets.token_hex(8)
        cls.raw_engine = create_engine(url, pool_pre_ping=True, pool_size=4)
        with cls.raw_engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{cls.schema}"'))
        cls.engine = cls.raw_engine.execution_options(schema_translate_map={None: cls.schema})
        cls.factory = sessionmaker(bind=cls.engine, expire_on_commit=False)
        Base.metadata.create_all(cls.engine)
        cls.storage = tempfile.TemporaryDirectory()
        cls.settings = Settings(str(url), "s"*48, b"k"*32, False, ("testserver",), Path(cls.storage.name), 1048576, None, None)

    @classmethod
    def tearDownClass(cls):
        with cls.raw_engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{cls.schema}" CASCADE'))
        cls.raw_engine.dispose()
        cls.storage.cleanup()

    def seed(self, count=3):
        marker = secrets.token_hex(8)
        cipher = SecretCipher(self.settings.master_key)
        with self.factory() as s, s.begin():
            user = AdminUser(email=f"{marker}@test.invalid", display_name="Synthetic", password_hash="unused", role="operator")
            stranger = AdminUser(email=f"other-{marker}@test.invalid", display_name="Other", password_hash="unused", role="operator")
            s.add_all([user, stranger])
            if not s.get(Platform, "rf1"):
                s.add(Platform(slug="rf1", name="Synthetic", runner="rf1"))
            s.flush()
            municipality = Municipality(slug=marker, name="Synthetic", platform_slug="rf1", max_workers=2, enabled=True, operational_status="ready")
            s.add(municipality)
            s.flush()
            dataset = Dataset(municipality_slug=marker, uploaded_by_id=user.id, original_filename="synthetic.csv", display_name="Synthetic", storage_path="unused", sha256=marker, row_count=count, status="ready")
            credentials = [PortalCredential(municipality_slug=marker, label=f"Account {i}", encryption_context=secrets.token_hex(16), username_ciphertext=b"unused", password_ciphertext=b"unused", portal_username=f"user{i}", login_identity=f"user{i}", status="active") for i in (1, 2)]
            tokens = [ApiToken(owner_id=u.id, name="Synthetic", token_prefix=marker[:8], token_hash=secrets.token_hex(32), scopes=["jobs:read", "jobs:write", "results:read", "exports:read", "exports:write", "datasets:read"]) for u in (user, stranger)]
            s.add_all([dataset, *credentials, *tokens])
            s.flush()
            for index in range(count):
                context = secrets.token_hex(16)
                s.add(DatasetRecord(dataset_id=dataset.id, row_number=index+2, encryption_context=context, cpf_ciphertext=cipher.encrypt("00123456797", context=f"record:{context}:cpf"), cpf_fingerprint=secrets.token_hex(32), cpf_last4="6797", source_ciphertext=cipher.encrypt(json.dumps({"CPF": "00123456797", "MATRICULA": str(index)}), context=f"record:{context}:source")))
            return dataset.id, user.id, [c.id for c in credentials], [token.id for token in tokens]

    def create(self, dataset, owner, accounts, key=None):
        with self.factory() as s, s.begin():
            job = create_execution(s, dataset_id=dataset, requested_by_id=owner, selected_credential_ids=accounts, max_parallel_accounts=2, idempotency_key=key)
            return job.id

    def test_concurrent_idempotent_creation_has_one_job_and_conflict_is_rejected(self):
        dataset, owner, accounts, _ = self.seed()
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = list(pool.map(lambda _: self.create(dataset, owner, accounts, "same-request"), range(2)))
        self.assertEqual(jobs[0], jobs[1])
        with self.factory() as s:
            self.assertEqual(1, s.scalar(select(func.count()).select_from(Job).where(Job.dataset_id == dataset)))
            with self.assertRaisesRegex(ValueError, "outra solicitação"):
                create_execution(s, dataset_id=dataset, requested_by_id=owner, selected_credential_ids=accounts, max_parallel_accounts=1, idempotency_key="same-request")

    def test_schedule_occurrence_once_overlap_skip_and_misfire_bounded(self):
        dataset, owner, accounts, _ = self.seed()
        now = datetime.now(UTC).replace(second=0, microsecond=0)
        with self.factory() as s, s.begin():
            schedule = create_schedule(s, name="Daily", dataset_id=dataset, requested_by_id=owner, selected_credential_ids=accounts, max_parallel_accounts=2, cron_expression="* * * * *")
            schedule.next_run_at = now
            schedule_id = schedule.id
        def tick():
            with self.factory() as s, s.begin():
                return process_due_schedules(s, now=now)
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(1, sum(pool.map(lambda _: tick(), range(2))))
        with self.factory() as s, s.begin():
            self.assertEqual(1, s.scalar(select(func.count()).select_from(ScheduleOccurrence).where(ScheduleOccurrence.schedule_id == schedule_id)))
            process_due_schedules(s, now=now + timedelta(minutes=1))
            occurrence = s.scalar(select(ScheduleOccurrence).where(ScheduleOccurrence.schedule_id == schedule_id).order_by(ScheduleOccurrence.id.desc()))
            self.assertEqual("skipped_overlap", occurrence.status)
            process_due_schedules(s, now=now + timedelta(days=3))
            occurrence = s.scalar(select(ScheduleOccurrence).where(ScheduleOccurrence.schedule_id == schedule_id).order_by(ScheduleOccurrence.id.desc()))
            self.assertEqual("skipped_late", occurrence.status)
            self.assertGreater(s.get(Schedule, schedule_id).next_run_at, now + timedelta(days=3))

    def test_export_snapshot_survives_changed_results_and_download_checksum(self):
        dataset, owner, accounts, _ = self.seed(1)
        job_id = self.create(dataset, owner, accounts)
        cipher = SecretCipher(self.settings.master_key)
        with self.factory() as s, s.begin():
            job = s.get(Job, job_id)
            item = s.scalar(select(JobItem).where(JobItem.job_id == job_id))
            item.status, item.outcome, item.attempts = "completed", "found", 1
            result = ConsultationResult(job_item_id=item.id, status="found", attempt_number=1, result_ciphertext=cipher.encrypt(json.dumps({"margins": {"disponivel": 500}}), context=f"result:{item.id}"))
            s.add(result)
            job.status, job.completed_items = "completed", 1
            artifact = request_export(s, self.settings, job, format="json")
            export_id = artifact.id
        with self.factory() as s, s.begin():
            result = s.scalar(select(ConsultationResult).join(JobItem).where(JobItem.job_id == job_id))
            result.result_ciphertext = cipher.encrypt(json.dumps({"margins": {"disponivel": 100}}), context=f"result:{result.job_item_id}")
            s.get(Job, job_id).result_version += 1
        self.assertTrue(process_one_export(self.factory, self.settings))
        with self.factory() as s:
            artifact = s.get(ExportArtifact, export_id)
            payload = read_export(self.settings, artifact)
            self.assertEqual(500, json.loads(payload)[0]["MARGEM_DISPONIVEL"])
            self.assertEqual(hashlib.sha256(payload).hexdigest(), artifact.sha256)
            self.assertNotIn(b"00123456797", Path(artifact.storage_path).read_bytes())

    def test_api_requires_scope_and_owner_and_paginates(self):
        dataset, owner, accounts, tokens = self.seed()
        job_id = self.create(dataset, owner, accounts)
        app = FastAPI()
        def principal_dependency(scope):
            def dependency(request: Request):
                token_id = request.headers.get("X-Test-Token")
                if not token_id:
                    raise HTTPException(401)
                with self.factory() as s:
                    token = s.get(ApiToken, int(token_id))
                    if not token or scope not in token.scopes:
                        raise HTTPException(403)
                    return SimpleNamespace(token_id=token.id, scopes=frozenset(token.scopes))
            return dependency
        def db():
            with self.factory() as session:
                yield session
        app.dependency_overrides[get_db] = db
        install_product_routes(app, self.settings, principal_dependency, lambda *_: "ok")
        with TestClient(app) as client:
            path = f"/api/v1/jobs/{job_id}/results"
            self.assertEqual(401, client.get(path).status_code)
            self.assertEqual(404, client.get(path, headers={"X-Test-Token": str(tokens[1])}).status_code)
            headers = {"X-Test-Token": str(tokens[0])}
            response = client.get(path + "?limit=2", headers=headers)
            self.assertEqual(200, response.status_code, response.text)
            first = response.json()
            self.assertEqual(2, first["count"])
            second = client.get(path + f'?limit=2&after_id={first["next_cursor"]}', headers=headers).json()
            self.assertEqual(1, second["count"])
            self.assertIsNone(second["next_cursor"])
            self.assertEqual(403, client.get("/api/v1/schedules", headers=headers).status_code)

    def test_webhook_outbox_deduplicates_signs_and_recovers_last_attempt_crash(self):
        dataset, owner, accounts, _ = self.seed()
        job_id = self.create(dataset, owner, accounts)
        with patch.dict(os.environ, {"WEBHOOK_ALLOWED_HOSTS": "example.test"}):
            with self.factory() as s, s.begin():
                _, secret = create_webhook(s, self.settings, owner_id=owner, name="Synthetic", url="https://example.test/hook")
                s.get(Job, job_id).status = "completed"
                s.flush()
                self.assertEqual(1, enqueue_webhooks(s))
                self.assertEqual(0, enqueue_webhooks(s))
            with patch("machine_admin.webhooks._post_pinned", return_value=204) as post:
                self.assertTrue(process_one_webhook(self.factory, self.settings))
                _, body, headers = post.call_args.args
                timestamp, signature = headers["X-Machine-Signature"].split(",")
                expected = hmac.new(secret.encode(), timestamp[2:].encode() + b"." + body, hashlib.sha256).hexdigest()
                self.assertEqual("v1=" + expected, signature)
                self.assertNotIn("cpf", body.decode().lower())
            with self.factory() as s, s.begin():
                message = s.scalar(select(NotificationOutbox).where(NotificationOutbox.job_id == job_id))
                self.assertEqual("sent", message.status)
                message.status = "processing"
                message.attempts = message.max_attempts
                message.locked_until = datetime.now(UTC) - timedelta(seconds=1)
                message_id = message.id
            self.assertFalse(process_one_webhook(self.factory, self.settings))
            with self.factory() as s:
                self.assertEqual("failed", s.get(NotificationOutbox, message_id).status)

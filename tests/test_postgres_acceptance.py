"""Acceptance against an isolated PostgreSQL, never portals or the production DB.

Run with MACHINE_TEST_DATABASE_URL pointing at a database whose name begins
machine_acceptance_. Each class owns and removes a random schema in that DB.
"""
from __future__ import annotations

import os
import secrets
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from machine_admin.models import (Base, ConsultationResult, CredentialLease, Dataset,
    DatasetRecord, Job, JobEvent, JobItem, Municipality, Platform, PortalCredential)
from machine_admin.queue import (acquire_credential, claim_job_items, complete_job_item,
    heartbeat_credential, release_credential, requeue_job_item, request_job_drain,
    apply_credential_report)


@unittest.skipUnless(os.getenv("MACHINE_TEST_DATABASE_URL"), "isolated PostgreSQL URL not configured")
class PostgresQueueAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        url = make_url(os.environ["MACHINE_TEST_DATABASE_URL"])
        if url.get_backend_name() != "postgresql" or not (url.database or "").startswith("machine_acceptance_"):
            raise RuntimeError("Refusing non-isolated database: name must start with machine_acceptance_")
        cls.schema = "worker_" + secrets.token_hex(8)
        cls.raw_engine = create_engine(url, pool_pre_ping=True, pool_size=4)
        with cls.raw_engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{cls.schema}"'))
        cls.engine = cls.raw_engine.execution_options(schema_translate_map={None: cls.schema})
        Base.metadata.create_all(cls.engine)

    @classmethod
    def tearDownClass(cls):
        with cls.raw_engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{cls.schema}" CASCADE'))
        cls.raw_engine.dispose()

    def seed(self, count=1):
        slug = "qa-" + secrets.token_hex(6)
        with Session(self.engine) as session, session.begin():
            if not session.get(Platform, "rf1"):
                session.add(Platform(slug="rf1", name="RF1 test", runner="rf1"))
                session.flush()
            session.add(Municipality(slug=slug, name="Synthetic", platform_slug="rf1", max_workers=2))
            session.flush()
            dataset = Dataset(municipality_slug=slug, original_filename="synthetic.xlsx",
                display_name="Synthetic", storage_path="/synthetic", sha256=secrets.token_hex(32),
                row_count=count, status="ready")
            session.add(dataset)
            credentials = [PortalCredential(municipality_slug=slug, label=f"Access {i}",
                encryption_context=secrets.token_hex(16), username_ciphertext=b"synthetic",
                password_ciphertext=b"synthetic", portal_username=f"user{i}", login_identity=f"user{i}")
                for i in (1, 2)]
            session.add_all(credentials)
            session.flush()
            job = Job(municipality_slug=slug, dataset_id=dataset.id, status="queued",
                total_items=count, max_parallel_accounts=2,
                selected_credential_ids=[credential.id for credential in credentials])
            session.add(job)
            records = [DatasetRecord(dataset_id=dataset.id, row_number=i + 1,
                encryption_context=secrets.token_hex(16), cpf_ciphertext=b"synthetic",
                cpf_fingerprint=secrets.token_hex(32), cpf_last4=f"{i%10000:04d}", source_ciphertext=b"synthetic")
                for i in range(count)]
            session.add_all(records)
            session.flush()
            session.add_all([JobItem(job_id=job.id, dataset_record_id=record.id) for record in records])
            return job.id, slug

    def acquire(self, job_id, slug, worker):
        with Session(self.engine) as session, session.begin():
            cred = acquire_credential(session, job_id=job_id, municipality_slug=slug, worker_id=worker)
            self.assertIsNotNone(cred)
            lease = session.get(CredentialLease, cred.id)
            return cred.id, lease.lease_token

    def claim(self, job_id, credential_id, worker, token):
        with Session(self.engine) as session, session.begin():
            rows = claim_job_items(session, job_id=job_id, credential_id=credential_id,
                worker_id=worker, credential_lease_token=token, batch_size=1)
            return [(item.id, item.lease_token) for item in rows]

    def complete(self, item_id, token, worker):
        with Session(self.engine) as session, session.begin():
            complete_job_item(session, item_id=item_id, lease_token=token, worker_id=worker,
                outcome="found", status="completed", result_ciphertext=b"synthetic-result")

    def test_1000_records_two_accounts_no_duplicate_persistence(self):
        job_id, slug = self.seed(1000)
        gate = threading.Barrier(2)
        processed = {"one": [], "two": []}
        def work(worker):
            credential_id, token = self.acquire(job_id, slug, worker)
            gate.wait(timeout=15)
            while True:
                try:
                    rows = self.claim(job_id, credential_id, worker, token)
                except ValueError:
                    break  # The other account may have just finalized the job.
                if not rows:
                    break
                item_id, item_token = rows[0]
                self.complete(item_id, item_token, worker)
                # Simulate response lost after commit, retry same acknowledgement.
                if len(processed[worker]) % 50 == 0:
                    self.complete(item_id, item_token, worker)
                processed[worker].append(item_id)
            with Session(self.engine) as session, session.begin():
                release_credential(session, worker_id=worker, credential_lease_token=token)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(work, worker) for worker in processed]
            for future in futures:
                future.result(timeout=180)
        self.assertTrue(processed["one"] and processed["two"])
        self.assertFalse(set(processed["one"]) & set(processed["two"]))
        self.assertEqual(1000, sum(map(len, processed.values())))
        with Session(self.engine) as session:
            job = session.get(Job, job_id)
            self.assertEqual(("completed", 1000, 0), (job.status, job.completed_items, job.failed_items))
            self.assertEqual(1000, session.scalar(select(func.count()).select_from(ConsultationResult)
                .join(JobItem).where(JobItem.job_id == job_id)))
            self.assertEqual(1000, session.scalar(select(func.count()).select_from(JobEvent)
                .where(JobEvent.job_id == job_id, JobEvent.event_type == "consulta.concluida")))

    def test_pause_waits_for_both_accounts_and_preserves_pending(self):
        job_id, slug = self.seed(3)
        one = self.acquire(job_id, slug, "pause-one")
        two = self.acquire(job_id, slug, "pause-two")
        row = self.claim(job_id, one[0], "pause-one", one[1])[0]
        with Session(self.engine) as session, session.begin():
            job = session.get(Job, job_id)
            self.assertEqual("pausing", request_job_drain(session, job))
        with self.assertRaises(ValueError):
            self.claim(job_id, two[0], "pause-two", two[1])
        self.complete(*row, "pause-one")
        with Session(self.engine) as session, session.begin():
            release_credential(session, worker_id="pause-one", credential_lease_token=one[1])
            self.assertEqual("pausing", session.get(Job, job_id).status)
        with Session(self.engine) as session, session.begin():
            release_credential(session, worker_id="pause-two", credential_lease_token=two[1])
            job = session.get(Job, job_id)
            self.assertEqual(("paused", 1), (job.status, job.completed_items))
            self.assertEqual(2, session.scalar(select(func.count()).select_from(JobItem).where(
                JobItem.job_id == job_id, JobItem.status == "pending")))

    def test_crashed_lease_reclaimed_and_old_generation_rejected(self):
        job_id, slug = self.seed()
        credential_id, token = self.acquire(job_id, slug, "restart-worker")
        old_item, old_token = self.claim(job_id, credential_id, "restart-worker", token)[0]
        with Session(self.engine) as session, session.begin():
            session.get(JobItem, old_item).lease_expires_at = datetime.now(UTC)-timedelta(seconds=1)
            session.get(CredentialLease, credential_id).expires_at = datetime.now(UTC)-timedelta(seconds=1)
        credential_id, new_token = self.acquire(job_id, slug, "restart-worker")
        with Session(self.engine) as session, session.begin():
            self.assertFalse(heartbeat_credential(session, worker_id="restart-worker", credential_lease_token=token))
        item_id, item_token = self.claim(job_id, credential_id, "restart-worker", new_token)[0]
        self.assertEqual(old_item, item_id)
        self.assertNotEqual(old_token, item_token)
        with self.assertRaises(ValueError):
            self.complete(old_item, old_token, "restart-worker")
        self.complete(item_id, item_token, "restart-worker")
        with Session(self.engine) as session:
            item = session.get(JobItem, item_id)
            self.assertEqual((2, 0), (item.attempts, item.retry_count))

    def test_bad_account_does_not_stop_healthy_account_or_consume_item(self):
        job_id, slug = self.seed(3)
        bad_id, bad_token = self.acquire(job_id, slug, "bad-account")
        healthy_id, healthy_token = self.acquire(job_id, slug, "healthy-account")
        item_id, item_token = self.claim(job_id, bad_id, "bad-account", bad_token)[0]
        with Session(self.engine) as session, session.begin():
            requeue_job_item(session, worker_id="bad-account", item_id=item_id,
                lease_token=item_token, reason="Password rejected", outcome="credential_error")
            apply_credential_report(session, session.get(PortalCredential, bad_id),
                outcome="invalid_credentials", stage="login")
            release_credential(session, worker_id="bad-account", credential_lease_token=bad_token)
        for _ in range(3):
            rows = self.claim(job_id, healthy_id, "healthy-account", healthy_token)
            self.assertEqual(1, len(rows))
            self.complete(*rows[0], "healthy-account")
        with Session(self.engine) as session:
            self.assertEqual("completed", session.get(Job, job_id).status)
            self.assertEqual(0, session.get(JobItem, item_id).retry_count)
            self.assertEqual("invalid", session.get(PortalCredential, bad_id).status)


if __name__ == "__main__":
    unittest.main()

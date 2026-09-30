"""Contrato HTTP completo contra PostgreSQL isolado, sem navegador de portal."""
import os
import secrets
import tempfile
import unittest
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from machine_admin.config import Settings
from machine_admin.db import get_db
from machine_admin.models import (AccessCheck, AdminUser, CredentialLease, DatasetRecord,
    IntegrationSecret, Job, JobItem, Municipality, PortalCredential, WorkerHeartbeat)
from machine_admin.security import SecretCipher
from machine_admin.services import issue_api_token
from machine_admin.web import create_app
from tests import test_postgres_acceptance as queue_acceptance


@unittest.skipUnless(os.getenv('MACHINE_TEST_DATABASE_URL'), 'isolated PostgreSQL URL not configured')
class WorkerHTTPPostgres(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        queue_acceptance.PostgresQueueAcceptance.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        queue_acceptance.PostgresQueueAcceptance.tearDownClass.__func__(cls)

    @contextmanager
    def fixture(self, count=2):
        job_id, slug = queue_acceptance.PostgresQueueAcceptance.seed(self, count=count)
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(os.environ['MACHINE_TEST_DATABASE_URL'], 'x'*48, b'k'*32,
                False, ('testserver',), Path(directory), 1024*1024, None, None)
            cipher = SecretCipher(settings.master_key)
            with Session(self.engine) as session, session.begin():
                job = session.get(Job, job_id)
                municipality = session.get(Municipality, slug)
                municipality.operational_status = 'ready'
                municipality.login_url = 'https://portal.invalid/login'
                municipality.query_url = 'https://portal.invalid/query'
                municipality.schedule_policy = {'weekdays': list(range(7)), 'start_hour':0, 'end_hour':24}
                for cred in session.scalars(select(PortalCredential).where(PortalCredential.municipality_slug == slug)):
                    cred.portal_password = 'synthetic-password'
                for record in session.scalars(select(DatasetRecord).where(DatasetRecord.dataset_id == job.dataset_id)):
                    record.cpf_ciphertext = cipher.encrypt('52998224725', context=f'record:{record.encryption_context}:cpf')
                if not session.get(IntegrationSecret, 'TWOCAPTCHA_API_KEY'):
                    session.add(IntegrationSecret(key='TWOCAPTCHA_API_KEY', value_ciphertext=b'unused'))
                worker = 'worker-http-' + secrets.token_hex(5)
                session.add(WorkerHeartbeat(worker_id=worker, platform_slug='rf1',
                    expires_at=datetime.now(UTC)+timedelta(minutes=5), health_status='healthy', activity_status='idle'))
                admin = AdminUser(email=f'{slug}@example.invalid', display_name='Synthetic', password_hash='unused', role='admin', active=True)
                session.add(admin)
                session.flush()
                _, token = issue_api_token(session, owner_id=admin.id, name='worker-test', scopes=['jobs:read','workers:execute'])
            app = create_app(settings)
            def database():
                with Session(self.engine, expire_on_commit=False) as session:
                    yield session
            app.dependency_overrides[get_db] = database
            client = TestClient(app, headers={'Authorization': 'Bearer '+token})
            try:
                yield client, job_id, slug, worker
            finally:
                client.close()

    def test_http_generation_pause_and_replay(self):
        with self.fixture() as (client, job_id, slug, worker):
            # Freeze one explicitly selected access, even though two healthy
            # logins exist in this agreement.
            with Session(self.engine) as session, session.begin():
                job = session.get(Job, job_id)
                selected = job.selected_credential_ids[-1]
                job.selected_credential_ids = [selected]
                job.max_parallel_accounts = 1
            acquired = client.post('/api/workers/credentials/acquire', json={
                'job_id':job_id, 'municipality_slug':slug, 'worker_id':worker})
            self.assertEqual(200, acquired.status_code, acquired.text)
            credential = acquired.json()
            self.assertEqual(selected, credential['credential_id'])
            common = {'worker_id':worker, 'credential_lease_token':credential['lease_token']}
            stale_claim = client.post('/api/workers/items/claim', json={**common,
                'credential_lease_token':'a'*48, 'job_id':job_id,
                'credential_id':credential['credential_id'], 'batch_size':1})
            self.assertEqual(409, stale_claim.status_code, stale_claim.text)
            claim = client.post('/api/workers/items/claim', json={**common,
                'job_id':job_id, 'credential_id':credential['credential_id'], 'batch_size':1})
            self.assertEqual(200, claim.status_code, claim.text)
            item = claim.json()['items'][0]
            self.assertEqual('52998224725', item['cpf'])
            with Session(self.engine) as session, session.begin():
                from machine_admin.queue import request_job_drain
                request_job_drain(session, session.get(Job,job_id))
            heartbeat = client.post('/api/workers/heartbeat', json=common)
            self.assertTrue(heartbeat.json()['drain_requested'])
            bad = client.post('/api/workers/heartbeat', json={**common, 'credential_lease_token':'a'*48})
            self.assertEqual(409, bad.status_code)
            body = {'worker_id':worker, 'item_id':item['item_id'], 'lease_token':item['lease_token'],
                    'status':'completed','outcome':'found','result_data':{'confirmed':{'cpf':'52998224725'}}}
            for _ in range(2):
                result = client.post('/api/workers/items/complete', json=body)
                self.assertEqual(200, result.status_code, result.text)
            released = client.post('/api/workers/release',json=common)
            self.assertEqual(200, released.status_code, released.text)
            with Session(self.engine) as session:
                job = session.get(Job,job_id)
                self.assertEqual('paused',job.status)
                self.assertEqual(1,job.completed_items)

    def test_access_check_for_invalid_login_consumes_no_base_records(self):
        with self.fixture() as (client, job_id, slug, worker):
            with Session(self.engine) as session, session.begin():
                # The login test must remain operable even when no usable
                # production account is currently available to scale the pool.
                for credential in session.scalars(select(PortalCredential)):
                    credential.status = 'invalid'
                job = session.get(Job, job_id)
                credential_id = job.selected_credential_ids[0]
                check = AccessCheck(credential_id=credential_id, status='queued')
                session.add(check)
                session.flush()
                check_id = check.id
            capacity = client.get('/api/workers/capacity?platform=rf1')
            self.assertEqual(200, capacity.status_code, capacity.text)
            self.assertGreaterEqual(capacity.json()['desired_workers'], 1)
            claimed = client.post('/api/workers/access-checks/claim', json={
                'worker_id':worker, 'platform_slug':'rf1', 'lease_seconds':120})
            self.assertEqual(200, claimed.status_code, claimed.text)
            self.assertEqual(check_id, claimed.json()['check_id'])
            lease_token = claimed.json()['credential']['lease_token']
            common = {'worker_id':worker, 'credential_lease_token':lease_token}
            heartbeat = client.post('/api/workers/heartbeat', json=common)
            self.assertEqual(200, heartbeat.status_code, heartbeat.text)
            self.assertFalse(heartbeat.json()['drain_requested'])
            # A competing executor cannot open a second test session.
            competing = client.post('/api/workers/access-checks/claim', json={
                'worker_id':'other-http-worker', 'platform_slug':'rf1'})
            self.assertIsNone(competing.json()['check_id'])
            late = client.post(f'/api/workers/access-checks/{check_id}/complete', json={
                **common, 'credential_lease_token':'b'*48, 'outcome':'success'})
            self.assertEqual(409, late.status_code, late.text)
            for _ in range(2):
                confirmed = client.post(f'/api/workers/access-checks/{check_id}/complete', json={
                    **common, 'outcome':'success', 'message':'Login confirmed'})
                self.assertEqual(200, confirmed.status_code, confirmed.text)
            with Session(self.engine) as session:
                credential = session.get(PortalCredential, credential_id)
                self.assertEqual('active', credential.status)
                self.assertIsNotNone(credential.last_validated_at)
                # Login success does not release the account before logout.
                self.assertIsNotNone(session.get(CredentialLease, credential_id))
                self.assertTrue(all(item.attempts == 0 for item in session.scalars(
                    select(JobItem).where(JobItem.job_id == job_id))))
            released = client.post('/api/workers/release', json=common)
            self.assertEqual(200, released.status_code, released.text)
            with Session(self.engine) as session:
                self.assertIsNone(session.get(CredentialLease, credential_id))

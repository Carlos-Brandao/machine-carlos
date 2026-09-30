"""Worker lifecycle regressions, without spending portal logins or captchas."""
from __future__ import annotations

import threading
import time
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from machine_admin.models import PortalCredential
from machine_admin.queue import apply_credential_report, complete_job_item, requeue_job_item
from run_worker import drain_excess_slots
from services.execution import ExecutionOutcome, OutcomeKind
from tests.test_queue_counter_deltas import QueueSession, _item, _job
from tests.test_worker_engine import FakeAPI, FakeAdapter, FakeSession, make_worker
from workers.api_client import WorkerAPIError
from workers.engine import GenericWorker


class WorkerReliabilityTests(unittest.TestCase):
    def test_downscale_drains_only_excess_and_does_not_drain_twice(self):
        stops = {1: threading.Event(), 2: threading.Event()}
        self.assertEqual([2], drain_excess_slots(stops, 1))
        self.assertFalse(stops[1].is_set())
        self.assertEqual([], drain_excess_slots(stops, 1))

    def test_bad_account_capacity_drop_preserves_the_other_busy_slot(self):
        stops = {1: threading.Event(), 2: threading.Event()}
        self.assertEqual([1], drain_excess_slots(stops, 1, preferred_slots={1}))
        self.assertFalse(stops[2].is_set())

    def test_login_is_renewed_before_it_returns(self):
        api = FakeAPI()
        seen = threading.Event()
        original = api.request
        def request(method, path, **kwargs):
            if path == "/api/workers/heartbeat":
                seen.set()
            return original(method, path, **kwargs)
        api.request = request
        class SlowLogin(FakeAdapter):
            lease_seconds = 0.2
            def open_session(self, credential):
                if not seen.wait(1):
                    raise AssertionError("no heartbeat while login blocked")
                return self.session
        session = FakeSession([])
        worker = GenericWorker(api, "worker-long-login", threading.Event(), SlowLogin(session))
        worker.run_once()
        self.assertTrue(seen.is_set())
        self.assertTrue(session.closed)

    def test_pause_while_consulting_saves_in_flight_result_then_closes(self):
        api = FakeAPI(claims=[[{"item_id": 1, "cpf": "123", "lease_token": "a" * 48}], []])
        paused = threading.Event()
        original = api.request
        def request(method, path, **kwargs):
            if path == "/api/workers/heartbeat" and paused.is_set():
                return {"drain_requested": True}
            return original(method, path, **kwargs)
        api.request = request
        class SlowSession(FakeSession):
            def consult(self, item):
                paused.set()
                time.sleep(0.12)
                return ExecutionOutcome.not_found(requested=item.requested)
            def close(self):
                self.closed = True
                self.assert_no_release = not api.calls_for("/api/workers/release")
        adapter = FakeAdapter(SlowSession([]))
        adapter.lease_seconds = 0.2
        worker = GenericWorker(api, "worker-draining", threading.Event(), adapter)
        worker.run_once()
        self.assertEqual(1, len(api.calls_for("/api/workers/items/claim")))
        self.assertEqual(1, len(api.calls_for("/api/workers/items/complete")))
        self.assertTrue(adapter.session.assert_no_release)

    def test_completion_network_retry_does_not_repeat_portal_consultation(self):
        api = FakeAPI(claims=[[{"item_id": 1, "cpf": "123", "lease_token": "a" * 48}], []])
        original = api.request
        attempts = []
        def request(method, path, **kwargs):
            if path == "/api/workers/items/complete":
                attempts.append(kwargs["json"])
                if len(attempts) == 1:
                    raise WorkerAPIError("response lost after commit")
            return original(method, path, **kwargs)
        api.request = request
        worker, session = make_worker(api, ExecutionOutcome.not_found(requested={"cpf": "123"}))
        worker.run_once()
        self.assertEqual(2, len(attempts))
        self.assertEqual(attempts[0], attempts[1])
        self.assertEqual([], session.outcomes)


class QueueOwnershipTests(unittest.TestCase):
    def test_expired_or_replaced_lease_cannot_complete(self):
        for item, token in (
            (_item(lease_token="new"), "old"),
            (_item(lease_token="old", lease_expires_at=datetime.now(UTC)-timedelta(seconds=1)), "old"),
        ):
            with self.subTest(token=token):
                with self.assertRaises(ValueError):
                    complete_job_item(QueueSession(_job(), item), worker_id="worker-1",
                        item_id=item.id, lease_token=token, status="completed", outcome="found",
                        result_ciphertext=b"result")

    def test_replayed_completion_does_not_increment_or_emit_twice(self):
        item = _item(lease_token="token")
        job = _job()
        session = QueueSession(job, item)
        args = dict(worker_id="worker-1", item_id=item.id, lease_token="token",
            status="completed", outcome="found", result_ciphertext=b"result")
        complete_job_item(session, **args)
        count = len(session.added)
        complete_job_item(session, **args)
        self.assertEqual(1, job.completed_items)
        self.assertEqual(count, len(session.added))

    def test_infrastructure_and_unstarted_work_do_not_consume_item_budget(self):
        for outcome, consume in (("credential_error", True), ("integration_unavailable", True),
                                 ("portal_unavailable", True), ("retryable_error", False)):
            with self.subTest(outcome=outcome, consume=consume):
                item = _item(attempts=50, retry_count=0, lease_token="token")
                requeue_job_item(QueueSession(_job(), item), worker_id="worker-1", item_id=item.id,
                    lease_token="token", outcome=outcome, reason="unavailable", consume_attempt=consume)
                self.assertEqual(0, item.retry_count)
                self.assertEqual("pending", item.status)

    def test_replay_requeue_does_not_consume_retry_twice(self):
        item = _item(lease_token="token")
        session = QueueSession(_job(), item)
        args = dict(worker_id="worker-1", item_id=item.id, lease_token="token", reason="timeout")
        requeue_job_item(session, **args)
        requeue_job_item(session, **args)
        self.assertEqual(1, item.retry_count)

    def test_third_unconfirmed_login_requires_operator_action(self):
        credential = PortalCredential(status="active", login_failure_count=0, failure_count=0)
        for _ in range(3):
            apply_credential_report(None, credential, outcome="transient_failure", stage="login")
        self.assertEqual("invalid", credential.status)
        self.assertEqual(3, credential.login_failure_count)
        apply_credential_report(None, credential, outcome="success")
        self.assertEqual("active", credential.status)
        self.assertEqual(0, credential.login_failure_count)


if __name__ == "__main__":
    unittest.main()

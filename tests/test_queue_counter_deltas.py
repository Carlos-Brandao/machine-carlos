"""Regressões dos contadores incrementais da fila."""

from __future__ import annotations

import inspect
import unittest
from datetime import UTC, datetime, timedelta

from sqlalchemy.dialects import postgresql

from machine_admin.models import ConsultationResult, Job, JobItem, JobItemAttempt
from machine_admin.queue import (
    _apply_job_counter_delta,
    complete_job_item,
    expire_exhausted_job_items,
    requeue_job_item,
)


def _sql(statement) -> str:
    return str(
        statement.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


def _job(**overrides) -> Job:
    values = {
        "id": 7,
        "municipality_slug": "boa-vista",
        "status": "running",
        "total_items": 1,
        "completed_items": 0,
        "failed_items": 0,
        "found_items": 0,
        "not_found_items": 0,
        "retryable_items": 0,
        "permanent_items": 0,
    }
    values.update(overrides)
    return Job(**values)


def _item(**overrides) -> JobItem:
    values = {
        "id": 19,
        "job_id": 7,
        "dataset_record_id": 31,
        "credential_id": 4,
        "status": "leased",
        "outcome": None,
        "attempts": 1,
        "max_attempts": 3,
        "lease_owner": "worker-1",
        "lease_expires_at": datetime.now(UTC) + timedelta(minutes=2),
        "last_attempt_at": datetime.now(UTC),
    }
    values.update(overrides)
    return JobItem(**values)


class QueueSession:
    """Session mínima que preserva a ordem/forma das consultas do hot path."""

    def __init__(
        self,
        job: Job,
        item: JobItem | None = None,
        *,
        items: list[JobItem] | None = None,
    ) -> None:
        self.job = job
        self.item = item
        self.items = items or []
        self.attempt = (
            JobItemAttempt(
                job_item_id=item.id,
                attempt_number=item.attempts,
                worker_id=item.lease_owner,
                credential_id=item.credential_id,
                status="started",
                started_at=item.last_attempt_at,
            )
            if item is not None
            else None
        )
        self.statements: list[str] = []
        self.added: list[object] = []
        self.flushes = 0

    def scalar(self, statement):
        sql = _sql(statement)
        self.statements.append(sql)
        if "SELECT job_items.job_id" in sql:
            return self.item.job_id if self.item else None
        if "FROM automation_jobs" in sql:
            return self.job
        if "FROM job_item_attempts" in sql:
            return self.attempt
        if "FROM consultation_results" in sql:
            return None
        if "FROM job_items" in sql:
            return self.item
        return None

    def scalars(self, statement):
        self.statements.append(_sql(statement))
        return list(self.items)

    def execute(self, statement):
        self.statements.append(_sql(statement))
        return None

    def add(self, value) -> None:
        self.added.append(value)

    def flush(self) -> None:
        self.flushes += 1


class CounterDeltaUnitTests(unittest.TestCase):
    def test_retry_category_change_has_zero_aggregate_delta(self) -> None:
        job = _job(retryable_items=1)

        _apply_job_counter_delta(
            job,
            old_status="leased",
            old_outcome="credential_error",
            new_status="pending",
            new_outcome="portal_unavailable",
        )

        self.assertEqual(1, job.retryable_items)
        self.assertEqual(0, job.completed_items)
        self.assertEqual(0, job.failed_items)

    def test_retry_outcome_is_replaced_by_success(self) -> None:
        job = _job(retryable_items=1)

        _apply_job_counter_delta(
            job,
            old_status="leased",
            old_outcome="retryable_error",
            new_status="completed",
            new_outcome="not_found",
        )

        self.assertEqual(0, job.retryable_items)
        self.assertEqual(1, job.not_found_items)
        self.assertEqual(1, job.completed_items)


class QueueHotPathDeltaTests(unittest.TestCase):
    def assert_no_group_by(self, session: QueueSession) -> None:
        self.assertFalse(
            any("GROUP BY" in statement for statement in session.statements),
            "O hot path não deve recontar todos os itens do job.",
        )

    def test_success_replaces_previous_retry_and_finalizes_job(self) -> None:
        job = _job(retryable_items=1)
        item = _item(outcome="portal_unavailable", attempts=2)
        session = QueueSession(job, item)

        completed = complete_job_item(
            session,  # type: ignore[arg-type]
            worker_id="worker-1",
            item_id=item.id,
            status="completed",
            outcome="found",
            result_ciphertext=b"resultado",
        )

        self.assertIs(completed, item)
        self.assertEqual("completed", item.status)
        self.assertEqual("found", item.outcome)
        self.assertEqual(1, job.completed_items)
        self.assertEqual(1, job.found_items)
        self.assertEqual(0, job.retryable_items)
        self.assertEqual("completed", job.status)
        self.assertIsNotNone(job.finished_at)
        result = next(
            value
            for value in session.added
            if isinstance(value, ConsultationResult)
        )
        self.assertEqual(2, result.attempt_number)
        self.assertIsNone(result.superseded_at)
        self.assert_no_group_by(session)

    def test_permanent_failure_creates_completed_with_errors(self) -> None:
        job = _job(
            total_items=2,
            completed_items=1,
            found_items=1,
        )
        item = _item()
        session = QueueSession(job, item)

        complete_job_item(
            session,  # type: ignore[arg-type]
            worker_id="worker-1",
            item_id=item.id,
            status="failed",
            outcome="permanent_error",
            result_ciphertext=b"erro",
        )

        self.assertEqual(1, job.failed_items)
        self.assertEqual(1, job.permanent_items)
        self.assertEqual("completed_with_errors", job.status)
        self.assert_no_group_by(session)

    def test_non_exhausted_requeue_keeps_one_retryable_outcome(self) -> None:
        job = _job(total_items=2, retryable_items=1)
        item = _item(outcome="credential_error", attempts=2, max_attempts=3)
        session = QueueSession(job, item)

        requeue_job_item(
            session,  # type: ignore[arg-type]
            worker_id="worker-1",
            item_id=item.id,
            reason="portal oscilou",
            outcome="integration_unavailable",
        )

        self.assertEqual("pending", item.status)
        self.assertEqual("integration_unavailable", item.outcome)
        self.assertEqual(1, job.retryable_items)
        self.assertEqual(0, job.failed_items)
        self.assertEqual("running", job.status)
        self.assert_no_group_by(session)

    def test_exhausted_requeue_adds_failed_and_retryable_once(self) -> None:
        job = _job()
        item = _item(attempts=3, max_attempts=3)
        session = QueueSession(job, item)

        requeue_job_item(
            session,  # type: ignore[arg-type]
            worker_id="worker-1",
            item_id=item.id,
            reason="timeout final",
        )

        self.assertEqual("failed", item.status)
        self.assertEqual(1, job.failed_items)
        self.assertEqual(1, job.retryable_items)
        self.assertEqual("failed", job.status)
        self.assert_no_group_by(session)

    def test_expire_multiple_items_applies_batch_deltas_and_finalizes(self) -> None:
        moment = datetime(2026, 8, 18, 19, 0, tzinfo=UTC)
        job = _job(
            total_items=3,
            completed_items=1,
            found_items=1,
            retryable_items=1,
        )
        leased = _item(
            id=20,
            attempts=3,
            max_attempts=3,
            outcome="portal_unavailable",
            lease_expires_at=moment - timedelta(seconds=1),
        )
        pending = _item(
            id=21,
            status="pending",
            lease_owner=None,
            attempts=3,
            max_attempts=3,
            outcome=None,
            lease_expires_at=None,
        )
        session = QueueSession(job, items=[leased, pending])

        changed = expire_exhausted_job_items(
            session,  # type: ignore[arg-type]
            job_id=job.id,
            now=moment,
        )

        self.assertEqual(2, changed)
        self.assertEqual(["failed", "failed"], [leased.status, pending.status])
        self.assertEqual(2, job.failed_items)
        self.assertEqual(2, job.retryable_items)
        self.assertEqual("completed_with_errors", job.status)
        self.assertEqual(moment, job.finished_at)
        self.assert_no_group_by(session)

    def test_hot_path_functions_do_not_call_full_reconciliation(self) -> None:
        for function in (
            complete_job_item,
            requeue_job_item,
            expire_exhausted_job_items,
        ):
            source = inspect.getsource(function)
            self.assertNotIn("refresh_job_counters(", source)
            self.assertNotIn("group_by(", source)


if __name__ == "__main__":
    unittest.main()

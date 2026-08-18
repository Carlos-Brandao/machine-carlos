"""Regressões para limites de fila, finalização concorrente e segredos."""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from sqlalchemy.dialects import postgresql

from machine_admin.config import Settings
from machine_admin.models import IntegrationSecret, Job
from machine_admin.queue import job_item_claim_statement, refresh_job_counters
from machine_admin.secret_store import (
    clear_secret_cache,
    configure_remote_secret_provider,
    get_runtime_secret,
)


def settings_for(storage_dir: Path) -> Settings:
    return Settings(
        database_url="postgresql://machine:test@localhost/machine",
        session_secret="s" * 48,
        master_key=b"k" * 32,
        cookie_secure=False,
        allowed_hosts=("testserver",),
        storage_dir=storage_dir,
        max_upload_bytes=1024 * 1024,
        bootstrap_admin_email=None,
        bootstrap_admin_password=None,
    )


def compiled_sql(statement) -> str:
    return str(
        statement.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


class QueueLimitRegressionTests(unittest.TestCase):
    def test_claim_never_leases_an_item_after_its_attempt_limit(self) -> None:
        sql = compiled_sql(
            job_item_claim_statement(
                job_id=9,
                now=datetime(2026, 8, 18, 15, 0, tzinfo=UTC),
                batch_size=10,
            )
        )

        self.assertIn(
            "job_items.attempts < job_items.max_attempts",
            sql,
            "Um lease expirado não pode furar o limite central de tentativas.",
        )


class FakeResult:
    def __init__(self, *, rows=None, scalar=None) -> None:
        self._rows = list(rows or [])
        self._scalar = scalar

    def all(self):
        return list(self._rows)

    def scalar_one_or_none(self):
        return self._scalar

    def scalar_one(self):
        return self._scalar


class LockTrackingSession:
    def __init__(self, job: Job) -> None:
        self.job = job
        self.calls: list[tuple[str, object, dict]] = []

    def _result_for(self, statement) -> FakeResult:
        sql = compiled_sql(statement)
        if "FROM automation_jobs" in sql:
            return FakeResult(scalar=self.job)
        if "job_items.status" in sql:
            return FakeResult(rows=[("completed", 2)])
        if "job_items.outcome" in sql:
            return FakeResult(rows=[("found", 2)])
        return FakeResult()

    def execute(self, statement):
        self.calls.append(("execute", statement, {}))
        return self._result_for(statement)

    def scalar(self, statement):
        self.calls.append(("scalar", statement, {}))
        return self._result_for(statement).scalar_one_or_none()

    def get(self, model, key, **kwargs):
        self.calls.append(("get", (model, key), kwargs))
        return self.job if model is Job and key == self.job.id else None


class JobFinalizationLockRegressionTests(unittest.TestCase):
    def test_job_row_is_locked_before_counting_and_finalizing(self) -> None:
        job = Job(
            id=41,
            municipality_slug="boa-vista",
            status="running",
            total_items=2,
            completed_items=0,
            failed_items=0,
            found_items=0,
            not_found_items=0,
            retryable_items=0,
            permanent_items=0,
        )
        session = LockTrackingSession(job)

        refresh_job_counters(session, job.id)  # type: ignore[arg-type]

        lock_index: int | None = None
        first_item_count_index: int | None = None
        for index, (kind, value, kwargs) in enumerate(session.calls):
            if kind == "get":
                model, _ = value
                if model is Job and kwargs.get("with_for_update"):
                    lock_index = index
                continue
            sql = compiled_sql(value)
            if "FROM automation_jobs" in sql and "FOR UPDATE" in sql:
                lock_index = index
            if "FROM job_items" in sql and first_item_count_index is None:
                first_item_count_index = index

        self.assertIsNotNone(
            lock_index,
            "refresh_job_counters deve serializar a linha do job com FOR UPDATE.",
        )
        self.assertIsNotNone(first_item_count_index)
        self.assertLess(
            lock_index,
            first_item_count_index,
            "O lock deve ocorrer antes das contagens para evitar uma finalização perdida.",
        )
        self.assertEqual("completed", job.status)
        self.assertIsNotNone(job.finished_at)


class RotatingSession:
    def __init__(self, secret: IntegrationSecret) -> None:
        self.secret = secret

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def get(self, model, key):
        if model is IntegrationSecret and key == self.secret.key:
            return self.secret
        return None


class RotatingSessionFactory:
    def __init__(self, secrets: list[IntegrationSecret]) -> None:
        self.secrets = iter(secrets)
        self.calls = 0

    def __call__(self) -> RotatingSession:
        self.calls += 1
        return RotatingSession(next(self.secrets))


class SecretRotationRegressionTests(unittest.TestCase):
    def tearDown(self) -> None:
        configure_remote_secret_provider(None)
        clear_secret_cache()

    def test_scoped_remote_reader_is_authoritative_for_minimal_workers(self) -> None:
        requested: list[str] = []

        def provider(key: str) -> str:
            requested.append(key)
            return "valor-rotacionado"

        configure_remote_secret_provider(provider)
        with patch.dict("os.environ", {"TWOCAPTCHA_API_KEY": "valor-antigo"}):
            self.assertEqual(
                "valor-rotacionado", get_runtime_secret("twocaptcha_api_key")
            )
        self.assertEqual(["TWOCAPTCHA_API_KEY"], requested)

    def test_remote_reader_fails_closed_instead_of_using_stale_environment(self) -> None:
        def unavailable(_key: str) -> str:
            raise OSError("offline")

        configure_remote_secret_provider(unavailable)
        with patch.dict("os.environ", {"TWOCAPTCHA_API_KEY": "valor-antigo"}):
            with self.assertRaisesRegex(RuntimeError, "segredo operacional"):
                get_runtime_secret("TWOCAPTCHA_API_KEY")

    def test_runtime_reader_observes_rotation_without_manual_cache_flush(self) -> None:
        old = IntegrationSecret(
            key="TWOCAPTCHA_API_KEY",
            value_ciphertext=b"old",
            key_version=1,
        )
        rotated = IntegrationSecret(
            key="TWOCAPTCHA_API_KEY",
            value_ciphertext=b"new",
            key_version=1,
        )
        factory = RotatingSessionFactory([old, rotated])
        clear_secret_cache()

        with tempfile.TemporaryDirectory() as directory:
            with (
                patch(
                    "machine_admin.secret_store.Settings.from_environment",
                    return_value=settings_for(Path(directory)),
                ),
                patch(
                    "machine_admin.secret_store.get_session_factory",
                    return_value=factory,
                ),
                patch(
                    "machine_admin.secret_store.decrypt_integration_secret",
                    side_effect=lambda secret, _settings: (
                        "token-antigo"
                        if secret.value_ciphertext == b"old"
                        else "token-rotacionado"
                    ),
                ),
            ):
                first = get_runtime_secret("TWOCAPTCHA_API_KEY")
                second = get_runtime_secret("TWOCAPTCHA_API_KEY")

        self.assertEqual("token-antigo", first)
        self.assertEqual("token-rotacionado", second)
        self.assertEqual(
            2,
            factory.calls,
            "Processos longos devem reler a fonte de verdade após a rotação.",
        )


if __name__ == "__main__":
    unittest.main()

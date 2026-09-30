"""Regressões dos casos limítrofes encontrados na revisão final."""

from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from sqlalchemy.dialects import postgresql
from starlette.requests import Request

from machine_admin.config import Settings
from machine_admin.datasets import _read_table
from machine_admin.exports import _export_row, merge_export_columns
from machine_admin.models import (
    AdminUser,
    ConsultationResult,
    DatasetRecord,
    IntegrationSecret,
    Job,
    JobEvent,
    JobItem,
    Municipality,
    NotificationOutbox,
    Platform,
)
from machine_admin.security import SecretCipher
from machine_admin.readiness import assess_municipality
from machine_admin.web import ApiPrincipal, create_app


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


def endpoint_for(app, path: str, method: str = "POST"):
    return next(
        route.endpoint
        for route in app.routes
        if getattr(route, "path", None) == path
        and method in getattr(route, "methods", set())
    )


def sql(statement) -> str:
    return str(
        statement.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


class CorruptedUploadRegressionTests(unittest.TestCase):
    def test_corrupted_xlsx_and_csv_become_the_same_friendly_validation_error(self) -> None:
        cases = (
            ("base.xlsx", b"isto nao e um arquivo zip do Excel"),
            ("base.csv", b"\xff\xfe\xfd\x00\x81"),
        )
        for filename, payload in cases:
            with self.subTest(filename=filename):
                with self.assertRaises(ValueError) as caught:
                    _read_table(filename, payload)
                message = str(caught.exception).lower()
                self.assertTrue(
                    any(
                        marker in message
                        for marker in ("não foi possível", "corrompido", "inválido")
                    ),
                    "A mensagem deve orientar o usuário, não expor o parser interno.",
                )
                self.assertNotIn("codec", message)
                self.assertNotIn("badzipfile", message)

    def test_csv_cell_limit_is_checked_before_pandas_allocates_the_table(self) -> None:
        with patch("machine_admin.datasets.MAX_DATASET_CELLS", 4):
            with self.assertRaisesRegex(ValueError, "2.000.000 de células"):
                _read_table(
                    "base.csv",
                    b"CPF,MATRICULA\n52998224725,A\n11144477735,B\n",
                )


class ExportCollisionRegressionTests(unittest.TestCase):
    def test_generated_columns_preserve_sources_and_skip_equivalent_values(self) -> None:
        merged = merge_export_columns(
            {
                "CPF": "529.982.247-25",
                "RETORNO_CPF": "valor original da base",
                "Status_Item": "status informado pelo cliente",
                "SAIDA_RETORNO_CPF": "coluna também importada",
            },
            {
                "RETORNO_CPF": "52998224725",
                "Status_Item": "completed",
            },
        )

        self.assertEqual("valor original da base", merged["RETORNO_CPF"])
        self.assertEqual("status informado pelo cliente", merged["Status_Item"])
        self.assertEqual("coluna também importada", merged["SAIDA_RETORNO_CPF"])
        self.assertNotIn("SAIDA_RETORNO_CPF_2", merged)
        self.assertEqual("completed", merged["SAIDA_Status_Item"])

    def test_superseded_result_is_preserved_but_not_mixed_into_new_export(self) -> None:
        cipher = SecretCipher(b"k" * 32)
        record = DatasetRecord(
            id=8,
            dataset_id=2,
            row_number=1,
            cpf_ciphertext=b"cpf",
            cpf_fingerprint="fingerprint",
            source_ciphertext=cipher.encrypt(
                json.dumps({"CPF": "52998224725"}),
                context="record:ctx:source",
            ),
            source_data={},
            encryption_context="ctx",
        )
        item = JobItem(
            id=15,
            job_id=3,
            dataset_record_id=record.id,
            status="failed",
            outcome="retryable_error",
            attempts=2,
            max_attempts=3,
        )
        result = ConsultationResult(
            job_item_id=item.id,
            status="found",
            result_ciphertext=cipher.encrypt(
                json.dumps({"Nome": "Resposta antiga"}),
                context=f"result:{item.id}",
            ),
            superseded_at=datetime.now(UTC),
        )

        exported = _export_row(cipher, item, record, result)

        self.assertEqual("52998224725", exported["CPF"])
        self.assertNotIn("RETORNO_NOME", exported)


class ReadinessSession:
    def __init__(self, platform: Platform, counts: tuple[int, ...]) -> None:
        self.platform = platform
        self.counts = iter(counts)
        self.statements: list[object] = []

    def get(self, model, key):
        if model is Platform and key == self.platform.slug:
            return self.platform
        if model is IntegrationSecret:
            return None
        return None

    def scalar(self, statement):
        self.statements.append(statement)
        return next(self.counts)


class AdapterVersionRegressionTests(unittest.TestCase):
    def test_online_worker_with_incompatible_adapter_does_not_unlock_agreement(self) -> None:
        platform = Platform(
            slug="consiglog",
            name="CONSIGX",
            runner="consiglog",
            start_hour=7,
            end_hour=21,
            enabled=True,
        )
        municipality = Municipality(
            slug="itabuna",
            name="Itabuna",
            platform_slug="consiglog",
            login_url="https://portal.invalid/login",
            query_url="https://portal.invalid/query",
            max_workers=1,
            enabled=True,
            operational_status="ready",
            timezone="America/Fortaleza",
            input_schema={"required": ["cpf"]},
            schedule_policy={
                "weekdays": [0, 1, 2, 3, 4],
                "start_hour": None,
                "end_hour": None,
            },
            adapter_version="consiglog.v2",
            settings_json={},
        )
        # credencial utilizável, um worker da processadora, zero workers v2.
        session = ReadinessSession(platform, (1, 1, 0))

        report = assess_municipality(session, municipality)

        self.assertFalse(report.can_start)
        self.assertEqual(0, report.online_workers)
        self.assertIn("adapter_version_mismatch", {issue.code for issue in report.issues})
        version_query = sql(session.statements[2])
        self.assertIn("worker_heartbeats.adapter_version = 'consiglog.v2'", version_query)


class ActionSession:
    def __init__(self, *, jobs=None, user=None, notification=None) -> None:
        self.jobs = list(jobs or [])
        self.user = user
        self.notification = notification
        self.statements: list[object] = []
        self.added: list[object] = []
        self.commits = 0
        self.rollbacks = 0

    def execute(self, statement):
        self.statements.append(statement)
        return type("Cursor", (), {"rowcount": 1})()

    def scalars(self, statement):
        self.statements.append(statement)
        return list(self.jobs)

    def scalar(self, statement):
        self.statements.append(statement)
        return self.notification

    def get(self, model, key):
        if model is AdminUser and self.user and key == self.user.id:
            return self.user
        return None

    def add(self, value):
        self.added.append(value)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def item_update_sql(statements: list[object]) -> list[str]:
    return [
        rendered
        for statement in statements
        for rendered in [sql(statement)]
        if rendered.startswith("UPDATE job_items SET")
    ]


class JobControlRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.app = create_app(settings_for(Path(cls.temporary.name)))
        cls.job_control_endpoint = endpoint_for(
            cls.app, "/admin/jobs/{job_id}/{action}"
        )
        cls.control_job = staticmethod(
            inspect.getclosurevars(cls.job_control_endpoint).nonlocals["control_job"]
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def test_pause_delegates_to_graceful_drain_without_releasing_the_account(self):
        job = Job(id=7, municipality_slug="boa-vista", status="running")
        session = ActionSession(notification=job)
        with patch("machine_admin.web.request_job_drain") as drain:
            self.control_job(session, job, "pause")
            drain.assert_called_once_with(session, job, cancel=False)
        self.assertEqual([], item_update_sql(session.statements))
        self.assertFalse(any(sql(s).startswith("DELETE FROM credential_leases") for s in session.statements))

    def test_bulk_clear_and_stop_share_control_job(self):
        for path in ("/api/jobs/queue/clear", "/api/jobs/running/stop"):
            endpoint = endpoint_for(self.app, path)
            self.assertIs(self.control_job, inspect.getclosurevars(endpoint).nonlocals["control_job"])

    def test_retry_creates_new_result_version_preserving_successes(self):
        job = Job(id=21, municipality_slug="boa-vista", status="failed", result_version=1)
        session = ActionSession()
        from unittest.mock import Mock
        session.scalar = Mock(side_effect=[job, None])
        self.control_job(session, job, "retry")
        rendered = [sql(statement) for statement in session.statements]
        self.assertTrue(any("UPDATE consultation_results_v2 SET superseded_at=" in x for x in rendered))
        self.assertEqual(2, job.result_version)
        self.assertEqual("queued", job.status)
        item_update = item_update_sql(session.statements)[0]
        self.assertIn("retry_count=0", item_update)
        self.assertIn("'failed', 'cancelled', 'leased'", item_update)
        self.assertNotIn("'completed'", item_update)



class ProcessingNotificationActionRegressionTests(unittest.TestCase):
    def test_processing_notification_rejects_manual_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(settings_for(Path(directory)))
        endpoint = endpoint_for(
            app, "/admin/notifications/{notification_id}/{action}"
        )
        user = AdminUser(
            id=1,
            email="operador@example.com",
            display_name="Operador",
            password_hash="unused",
            role="operator",
            active=True,
            session_version=1,
        )
        notification = NotificationOutbox(
            id=20,
            deduplication_key="processing-20",
            channel="webhook",
            status="processing",
            payload_json={"type": "job_result"},
            attempts=1,
            max_attempts=5,
            locked_by="notifier-20",
        )
        session = ActionSession(user=user, notification=notification)
        request = Request(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "POST",
                "scheme": "https",
                "path": "/admin/notifications/20/retry",
                "raw_path": b"/admin/notifications/20/retry",
                "query_string": b"",
                "headers": [],
                "client": ("127.0.0.1", 12345),
                "server": ("testserver", 443),
                "session": {"user_id": 1, "session_version": 1, "csrf": "csrf-test"},
            }
        )

        response = endpoint(
            notification_id=20,
            action="retry",
            request=request,
            csrf="csrf-test",
            session=session,
        )

        self.assertEqual(303, response.status_code)
        self.assertEqual("processing", notification.status)
        self.assertEqual(0, session.commits)
        self.assertEqual(1, session.rollbacks)
        self.assertIn("andamento", request.session["flash"]["message"])


class CapacitySession:
    def __init__(
        self, municipalities: list[Municipality], credential_counts: tuple[int, ...]
    ) -> None:
        self.municipalities = municipalities
        self.credential_counts = iter(credential_counts)
        self.statements: list[object] = []

    def scalars(self, statement):
        self.statements.append(statement)
        return list(self.municipalities)

    def scalar(self, statement):
        self.statements.append(statement)
        return next(self.credential_counts)


class AdaptiveCapacityRegressionTests(unittest.TestCase):
    def test_worker_capacity_is_derived_from_database_agreements_and_capped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(settings_for(Path(directory)))
        endpoint = endpoint_for(app, "/api/workers/capacity", method="GET")
        agreements = [
            Municipality(
                slug="gov-am",
                name="GOV AM",
                platform_slug="facil",
                max_workers=7,
                enabled=True,
                operational_status="ready",
                timezone="America/Manaus",
                input_schema={},
                schedule_policy={"weekdays": [0]},
                settings_json={},
            ),
            Municipality(
                slug="paulista",
                name="Paulista",
                platform_slug="facil",
                max_workers=15,
                enabled=True,
                operational_status="testing",
                timezone="America/Fortaleza",
                input_schema={},
                schedule_policy={"weekdays": [0]},
                settings_json={},
            ),
        ]
        session = CapacitySession(agreements, (7, 15, 0))

        payload = endpoint(
            platform="facil",
            _=ApiPrincipal("reader", frozenset({"jobs:read"})),
            session=session,
        )

        self.assertEqual(20, payload["desired_workers"])
        self.assertEqual("database", payload["source"])
        self.assertEqual({"gov-am", "paulista"}, {item["slug"] for item in payload["agreements"]})
        query = sql(session.statements[0])
        self.assertIn("municipalities.max_workers", query)
        self.assertIn("municipalities.operational_status IN ('testing', 'ready', 'degraded')", query)


if __name__ == "__main__":
    unittest.main()

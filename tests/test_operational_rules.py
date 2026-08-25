"""Regressões das políticas operacionais sem serviços externos reais."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from sqlalchemy.dialects import postgresql

from machine_admin.config import Settings
from machine_admin.exports import build_job_export
from machine_admin.models import (
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
from machine_admin.notifications import (
    _mark_failure,
    claim_notification,
    deliver_notification,
    enqueue_job_result,
)
from machine_admin.queue import (
    _apply_retry,
    _retry_delay,
    credential_candidate_statement,
    job_item_claim_statement,
)
from machine_admin.readiness import assess_municipality
from machine_admin.scheduling import schedule_decision
from machine_admin.security import SecretCipher
from services.execution import ExecutionOutcome, OutcomeKind


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


def municipality_for(
    platform: Platform,
    *,
    slug: str = "teste",
    timezone: str = "UTC",
    schedule_policy: dict | None = None,
) -> Municipality:
    return Municipality(
        slug=slug,
        name="Convênio de teste",
        platform_slug=platform.slug,
        login_url="https://portal.invalid/login",
        query_url="https://portal.invalid/query",
        max_workers=1,
        enabled=True,
        operational_status="ready",
        timezone=timezone,
        input_schema={"required": ["cpf"]},
        schedule_policy=schedule_policy
        or {"weekdays": [0, 1, 2, 3, 4], "start_hour": None, "end_hour": None},
        settings_json={},
    )


class SchedulingTests(unittest.TestCase):
    def test_boa_vista_policy_is_really_24_by_7(self) -> None:
        platform = Platform(
            slug="rf1", name="RF1", runner="rf1", start_hour=0, end_hour=24, enabled=True
        )
        municipality = municipality_for(
            platform,
            slug="boa-vista",
            timezone="America/Boa_Vista",
            schedule_policy={
                "weekdays": [0, 1, 2, 3, 4, 5, 6],
                "start_hour": 0,
                "end_hour": 24,
            },
        )

        # Domingo, 03:30 no fuso de Boa Vista.
        decision = schedule_decision(
            municipality,
            platform,
            moment=datetime(2026, 8, 16, 7, 30, tzinfo=UTC),
        )

        self.assertTrue(decision.allowed)
        self.assertIsNone(decision.next_start_at)
        self.assertEqual(tuple(range(7)), decision.weekdays)
        self.assertEqual((0, 24), (decision.start_hour, decision.end_hour))

    def test_agreement_inherits_processor_hours_and_calculates_next_start(self) -> None:
        platform = Platform(
            slug="facil",
            name="FACILCONSIG",
            runner="facil",
            start_hour=7,
            end_hour=21,
            enabled=True,
        )
        municipality = municipality_for(platform)

        before_open = schedule_decision(
            municipality,
            platform,
            moment=datetime(2026, 8, 17, 6, 30, tzinfo=UTC),  # segunda
        )
        at_close = schedule_decision(
            municipality,
            platform,
            moment=datetime(2026, 8, 17, 21, 0, tzinfo=UTC),
        )

        self.assertFalse(before_open.allowed)
        self.assertEqual(datetime(2026, 8, 17, 7, 0, tzinfo=UTC), before_open.next_start_at)
        self.assertEqual((7, 21), (before_open.start_hour, before_open.end_hour))
        self.assertFalse(at_close.allowed)
        self.assertEqual(datetime(2026, 8, 18, 7, 0, tzinfo=UTC), at_close.next_start_at)


class ReadinessSession:
    def __init__(self, platform: Platform, counts: tuple[int, int]) -> None:
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


class ReadinessTests(unittest.TestCase):
    def test_expired_or_unbounded_cooldown_is_consistently_usable(self) -> None:
        platform = Platform(
            slug="consiglog",
            name="CONSIGX",
            runner="consiglog",
            start_hour=7,
            end_hour=21,
            enabled=True,
        )
        municipality = municipality_for(platform, slug="itabuna")
        session = ReadinessSession(platform, counts=(1, 1))

        report = assess_municipality(session, municipality)
        readiness_sql = str(
            session.statements[0].compile(
                dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
            )
        )
        lease_sql = str(
            credential_candidate_statement(
                municipality_slug="itabuna", now=datetime(2026, 8, 18, tzinfo=UTC)
            ).compile(
                dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
            )
        )

        self.assertTrue(report.can_start)
        # A prontidão e a aquisição precisam aplicar a mesma semântica:
        # cooldown nulo ou vencido volta a ser utilizável.
        for sql in (readiness_sql, lease_sql):
            self.assertIn("portal_credentials.status = 'cooldown'", sql)
            self.assertIn("portal_credentials.cooldown_until IS NULL", sql)
            self.assertIn("portal_credentials.cooldown_until <=", sql)


class RowResult:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self.rows = rows

    def all(self) -> list[tuple[object, ...]]:
        return self.rows


class ExportSession:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self.rows = rows

    def execute(self, _statement) -> RowResult:
        return RowResult(self.rows)


class FullExportTests(unittest.TestCase):
    def test_portal_result_never_overwrites_the_imported_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = settings_for(Path(directory))
            cipher = SecretCipher(settings.master_key)
            record = DatasetRecord(
                id=4,
                dataset_id=2,
                row_number=1,
                encryption_context="record-4",
                cpf_ciphertext=b"unused",
                cpf_fingerprint="fingerprint",
                cpf_last4="4725",
                registration="ABC",
                source_ciphertext=cipher.encrypt(
                    json.dumps(
                        {"CPF": "529.982.247-25", "MATRICULA": "ABC", "Nome": "Importado"}
                    ),
                    context="record:record-4:source",
                ),
                source_data={},
            )
            item = JobItem(
                id=8,
                job_id=3,
                dataset_record_id=4,
                status="completed",
                outcome="found",
                attempts=1,
                max_attempts=3,
            )
            result_payload = {
                "outcome": "found",
                "confirmed": {"cpf": "52998224725", "registration": "ABC"},
                "person": {"nome": "Retornado"},
                "raw": {"CPF": "00000000000", "Nome": "Portal bruto"},
            }
            result = ConsultationResult(
                job_item_id=8,
                status="found",
                result_ciphertext=cipher.encrypt(
                    json.dumps(result_payload), context="result:8"
                ),
            )

            workbook, count = build_job_export(
                ExportSession([(item, record, result)]),  # type: ignore[arg-type]
                settings,
                job_id=3,
            )
            frame = pd.read_excel(io.BytesIO(workbook))
            row = frame.iloc[0]

            self.assertEqual(1, count)
            self.assertEqual("529.982.247-25", row["CPF"])
            self.assertEqual("Importado", row["Nome"])
            self.assertEqual("00000000000", str(row["RETORNO_CPF"]).zfill(11))
            self.assertEqual("Retornado", row["SERVIDOR_NOME"])
            self.assertEqual("found", row["Resultado_Item"])


class RetryPolicyTests(unittest.TestCase):
    def test_retry_backoff_is_centralized_bounded_and_clears_the_lease(self) -> None:
        item = JobItem(
            id=10,
            job_id=1,
            dataset_record_id=2,
            credential_id=9,
            status="leased",
            attempts=1,
            max_attempts=3,
            lease_owner="worker-1",
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=2),
        )
        before = datetime.now(UTC)

        exhausted = _apply_retry(
            item,
            outcome="portal_unavailable",
            error_code="timeout",
            error_message="Portal lento",
            retry_after_seconds=1,  # o backend impõe piso de segurança
        )

        self.assertFalse(exhausted)
        self.assertEqual("pending", item.status)
        self.assertEqual("portal_unavailable", item.outcome)
        self.assertIsNone(item.credential_id)
        self.assertIsNone(item.lease_owner)
        self.assertIsNone(item.lease_expires_at)
        self.assertGreaterEqual(item.next_attempt_at, before + timedelta(seconds=5))
        self.assertEqual(5, _retry_delay(item, 0))
        self.assertEqual(86_400, _retry_delay(item, 999_999))

    def test_retry_limit_becomes_a_terminal_failure(self) -> None:
        item = JobItem(
            id=10,
            job_id=1,
            dataset_record_id=2,
            status="leased",
            attempts=3,
            max_attempts=3,
            lease_owner="worker-1",
        )

        exhausted = _apply_retry(
            item,
            outcome="retryable_error",
            error_code="timeout",
            error_message="Três tentativas",
            retry_after_seconds=None,
        )

        self.assertTrue(exhausted)
        self.assertEqual("failed", item.status)
        self.assertIsNotNone(item.finished_at)
        self.assertIsNone(item.next_attempt_at)

    def test_claim_only_selects_retries_whose_backoff_has_elapsed(self) -> None:
        sql = str(
            job_item_claim_statement(
                job_id=7, now=datetime(2026, 8, 18, tzinfo=UTC), batch_size=500
            ).compile(
                dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
            )
        )

        self.assertIn("job_items.next_attempt_at IS NULL", sql)
        self.assertIn("job_items.next_attempt_at <=", sql)
        self.assertIn("LIMIT 100", sql)
        self.assertIn("FOR UPDATE SKIP LOCKED", sql)


class ExecutionOutcomeTests(unittest.TestCase):
    def test_outcome_categories_have_unambiguous_terminal_semantics(self) -> None:
        found = ExecutionOutcome.found(
            requested={"cpf": "529.982.247-25", "registration": "ab-01"},
            confirmed={"cpf": "52998224725", "registration": "AB01"},
            raw={"Status_Robo": "Sucesso"},
        )
        not_found = ExecutionOutcome.not_found(requested={"cpf": "52998224725"})
        retryable = ExecutionOutcome.error(
            OutcomeKind.PORTAL_UNAVAILABLE,
            code="timeout",
            message="Portal indisponível",
            stage="consultation",
            retry_after_seconds=60,
            end_session=True,
        )

        self.assertTrue(found.is_success)
        self.assertFalse(found.should_requeue)
        self.assertTrue(not_found.is_success)
        self.assertFalse(not_found.should_requeue)
        self.assertFalse(retryable.is_success)
        self.assertTrue(retryable.should_requeue)
        self.assertTrue(retryable.end_session)
        self.assertEqual("timeout", retryable.to_payload()["error"]["code"])


class OutboxSession:
    def __init__(self, scalar_result=None) -> None:
        self.scalar_result = scalar_result
        self.statements: list[object] = []
        self.added: list[object] = []
        self.flushes = 0

    def scalar(self, statement):
        self.statements.append(statement)
        return self.scalar_result

    def add(self, value: object) -> None:
        self.added.append(value)

    def flush(self) -> None:
        self.flushes += 1


class NotificationOutboxTests(unittest.TestCase):
    def test_enqueue_requires_explicit_recipient_and_is_idempotent(self) -> None:
        no_recipient = Job(id=1, municipality_slug="boa-vista", status="completed")
        self.assertIsNone(enqueue_job_result(OutboxSession(), no_recipient))  # type: ignore[arg-type]

        finished = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
        job = Job(
            id=2,
            municipality_slug="boa-vista",
            status="completed",
            telegram_chat_id=998877,
            finished_at=finished,
        )
        session = OutboxSession()
        created = enqueue_job_result(session, job)  # type: ignore[arg-type]

        self.assertEqual("998877", created.recipient)
        self.assertEqual("pending", created.status)
        self.assertIn(":2:", created.deduplication_key)
        self.assertEqual(
            "2026-08-18_09-00-00_Boa_Vista_MargemConsultada.xlsx",
            created.payload_json["filename"],
        )
        self.assertEqual([created], session.added)

        session.scalar_result = created
        same = enqueue_job_result(session, job)  # type: ignore[arg-type]
        self.assertIs(created, same)
        self.assertEqual([created], session.added)

    def test_claim_is_atomic_and_ignores_delayed_or_exhausted_messages(self) -> None:
        session = OutboxSession()
        self.assertIsNone(claim_notification(session, worker_id="notify-1"))  # type: ignore[arg-type]
        sql = str(session.statements[0].compile(dialect=postgresql.dialect()))

        self.assertIn("notification_outbox.attempts < notification_outbox.max_attempts", sql)
        self.assertIn("notification_outbox.next_attempt_at", sql)
        self.assertIn("FOR UPDATE SKIP LOCKED", sql)

    def test_failure_retries_with_backoff_and_stops_at_the_limit(self) -> None:
        retrying = NotificationOutbox(
            deduplication_key="retry",
            channel="telegram",
            status="processing",
            payload_json={},
            attempts=2,
            max_attempts=5,
            locked_by="notify-1",
        )
        before = datetime.now(UTC)
        _mark_failure(retrying, RuntimeError("temporário"))
        self.assertEqual("retry", retrying.status)
        self.assertIsNone(retrying.locked_by)
        self.assertGreaterEqual(retrying.next_attempt_at, before + timedelta(seconds=60))

        exhausted = NotificationOutbox(
            deduplication_key="failed",
            channel="telegram",
            status="processing",
            payload_json={},
            attempts=5,
            max_attempts=5,
        )
        _mark_failure(exhausted, RuntimeError("definitivo"))
        self.assertEqual("failed", exhausted.status)
        self.assertIsNone(exhausted.next_attempt_at)

    def test_successful_delivery_targets_only_the_job_recipient(self) -> None:
        class FakeNotifier:
            enabled = True

            def __init__(self) -> None:
                self.delivered = False
                self.filename = ""

            def document(self, path: Path, caption: str) -> bool:
                self.filename = path.name
                self.delivered = path.read_bytes() == b"xlsx" and "Resultado" in caption
                return self.delivered

        notification = NotificationOutbox(
            id=9,
            deduplication_key="send",
            job_id=5,
            channel="telegram",
            recipient="123456",
            status="processing",
            payload_json={
                "type": "job_result",
                "filename": "../../resultado.xlsx",
                "caption": "Resultado final",
            },
            attempts=1,
            max_attempts=5,
            locked_by="notify-1",
        )
        notifier = FakeNotifier()
        session = OutboxSession()
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch("machine_admin.notifications.build_job_export", return_value=(b"xlsx", 1)),
                patch("machine_admin.notifications.TelegramNotifier.for_chat", return_value=notifier) as for_chat,
            ):
                deliver_notification(
                    session, settings_for(Path(directory)), notification  # type: ignore[arg-type]
                )

        for_chat.assert_called_once_with(123456)
        self.assertTrue(notifier.delivered)
        self.assertEqual("resultado.xlsx", notifier.filename)
        self.assertEqual("sent", notification.status)
        self.assertIsNotNone(notification.sent_at)
        self.assertIsNone(notification.locked_by)
        self.assertTrue(any(isinstance(value, JobEvent) for value in session.added))


if __name__ == "__main__":
    unittest.main()

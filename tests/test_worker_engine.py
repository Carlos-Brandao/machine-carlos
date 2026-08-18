"""Testes do motor genérico sem navegador, portal ou PostgreSQL reais."""

from __future__ import annotations

import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch

import requests

from facil.facil import SearchResponseUnconfirmed
from rf1.rf1 import RF1NotFound
from services.captcha import CaptchaError
from services.execution import ExecutionOutcome, OutcomeKind
from workers.adapters.facil import _login_without_side_effects
from workers.api_client import WorkerAPIClient, WorkerAPIConflict, WorkerAPIError
from workers.engine import CredentialPayload, GenericWorker, WorkItem
from workers.consiglog_worker import ConsiglogWorker
from workers.facil_worker import FacilWorker
from workers.registry import (
    ADAPTERS,
    configured_platforms,
    create_adapter,
    default_worker_count,
)
from workers.rf1_worker import RF1Worker


class FakeSession:
    def __init__(self, outcomes: list[ExecutionOutcome]) -> None:
        self.outcomes = outcomes
        self.closed = False

    def consult(self, item: WorkItem) -> ExecutionOutcome:
        return self.outcomes.pop(0)

    def close(self) -> None:
        self.closed = True


class FakeAdapter:
    platform = "rf1"
    batch_size = 1
    lease_seconds = 600

    def __init__(self, session: FakeSession) -> None:
        self.session = session

    def open_session(self, credential: CredentialPayload) -> FakeSession:
        return self.session

    def classify_exception(self, exc, *, stage, item=None) -> ExecutionOutcome:
        return ExecutionOutcome.error(
            OutcomeKind.PERMANENT_ERROR,
            requested=item.requested if item else {},
            code=type(exc).__name__,
            message=str(exc),
            stage=stage,
        )


class FakeAPI:
    def __init__(self, claims: list[list[dict]] | None = None) -> None:
        self.claims = list(claims or [])
        self.calls: list[tuple[str, str, dict]] = []
        self.status = {
            "running": [
                {
                    "id": 7,
                    "prefeitura": "boa-vista",
                    "platform": "rf1",
                    "status": "running",
                    "executable": True,
                }
            ],
            "queued": [],
        }

    def request(self, method: str, path: str, **kwargs):
        self.calls.append((method, path, kwargs))
        if path == "/api/jobs/status":
            return self.status
        if path == "/api/workers/credentials/acquire":
            return {
                "credential_id": 3,
                "username": "user",
                "password": "password",
                "login_url": "https://portal/login",
                "query_url": "https://portal/query",
                "settings": {},
            }
        if path == "/api/workers/items/claim":
            return {"items": self.claims.pop(0) if self.claims else []}
        return {"ok": True}

    def calls_for(self, path: str) -> list[dict]:
        return [kwargs for _, called_path, kwargs in self.calls if called_path == path]


def make_worker(api: FakeAPI, outcome: ExecutionOutcome) -> tuple[GenericWorker, FakeSession]:
    session = FakeSession([outcome])
    return (
        GenericWorker(
            api=api,  # type: ignore[arg-type]
            worker_id="worker-test-1",
            stop_event=threading.Event(),
            adapter=FakeAdapter(session),
            poll_seconds=0,
        ),
        session,
    )


class ExecutionContractTests(unittest.TestCase):
    def test_payload_contains_canonical_envelope_and_legacy_raw(self) -> None:
        outcome = ExecutionOutcome.found(
            requested={"cpf": "123"},
            confirmed={"cpf": "123"},
            person={"name": "Pessoa"},
            margins={"loan": "10,00"},
            raw={"Nome": "Pessoa", "Margem": "10,00"},
        )
        payload = outcome.to_payload()
        self.assertEqual("found", payload["outcome"])
        self.assertEqual({"cpf": "123"}, payload["requested"])
        self.assertEqual({"cpf": "123"}, payload["confirmed"])
        self.assertEqual("Pessoa", payload["Nome"])
        self.assertEqual("10,00", payload["raw"]["Margem"])

    def test_found_rejects_an_unconfirmed_or_different_cpf(self) -> None:
        for confirmed in ({}, {"cpf": "999"}):
            with self.subTest(confirmed=confirmed):
                with self.assertRaisesRegex(ValueError, "CPF solicitado confirmado"):
                    ExecutionOutcome.found(
                        requested={"cpf": "123"},
                        confirmed=confirmed,
                        raw={},
                    )

    def test_found_requires_registration_only_when_it_was_requested(self) -> None:
        with self.assertRaisesRegex(ValueError, "matrícula solicitada confirmada"):
            ExecutionOutcome.found(
                requested={"cpf": "123", "registration": "AB-01"},
                confirmed={"cpf": "123", "registration": ""},
                raw={},
            )
        outcome = ExecutionOutcome.found(
            requested={"cpf": "123", "registration": None},
            confirmed={"cpf": "123", "registration": None},
            raw={},
        )
        self.assertEqual(OutcomeKind.FOUND, outcome.kind)

    def test_only_transactional_adapters_are_available(self) -> None:
        self.assertTrue(ADAPTERS["rf1"].available)
        self.assertTrue(ADAPTERS["facil"].available)
        self.assertTrue(ADAPTERS["consiglog"].available)
        self.assertFalse(ADAPTERS["safeconsig"].available)
        self.assertFalse(ADAPTERS["grid"].available)
        with patch.dict("os.environ", {"WORKER_PLATFORMS": "rf1,facil"}):
            self.assertEqual(("rf1", "facil"), configured_platforms())
        with patch.dict("os.environ", {"WORKER_PLATFORMS": "grid"}):
            with self.assertRaisesRegex(ValueError, "indisponíveis"):
                configured_platforms()

    def test_platform_pool_capacity_sums_its_agreements(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(3, default_worker_count("rf1"))
            self.assertGreater(default_worker_count("facil"), 1)

    def test_legacy_worker_entrypoints_are_thin_generic_compatibility_shims(self) -> None:
        for worker_class in (RF1Worker, FacilWorker, ConsiglogWorker):
            with self.subTest(worker=worker_class.__name__):
                self.assertTrue(issubclass(worker_class, GenericWorker))
        self.assertNotIn(RF1Worker, FacilWorker.__mro__[1:])
        self.assertNotIn(RF1Worker, ConsiglogWorker.__mro__[1:])

    def test_unexpected_adapter_bugs_are_retryable_not_false_permanent_failures(self) -> None:
        for platform in ("rf1", "facil", "consiglog"):
            with self.subTest(platform=platform):
                outcome = create_adapter(platform).classify_exception(
                    RuntimeError("unexpected"), stage="consultation"
                )
                self.assertEqual(OutcomeKind.RETRYABLE_ERROR, outcome.kind)
                self.assertTrue(outcome.end_session)

    def test_negative_results_require_explicit_portal_evidence(self) -> None:
        rf1 = create_adapter("rf1").classify_exception(
            RF1NotFound("Servidor não localizado."),
            stage="consultation",
        )
        facil = create_adapter("facil").classify_exception(
            SearchResponseUnconfirmed("Resposta sem confirmação."),
            stage="consultation",
        )

        self.assertEqual(OutcomeKind.NOT_FOUND, rf1.kind)
        self.assertEqual(OutcomeKind.RETRYABLE_ERROR, facil.kind)

    def test_captcha_http_failures_are_integration_outages(self) -> None:
        for platform in ("rf1", "facil"):
            with self.subTest(platform=platform):
                outcome = create_adapter(platform).classify_exception(
                    requests.Timeout("2captcha offline"), stage="login"
                )
                self.assertEqual(OutcomeKind.INTEGRATION_UNAVAILABLE, outcome.kind)
                self.assertEqual("captcha_unavailable", outcome.error_code)


class WorkerAPIClientTests(unittest.TestCase):
    def test_network_errors_are_normalized_for_the_worker_recovery_loop(self) -> None:
        client = WorkerAPIClient("http://backend.internal", "token")
        client.session.request = Mock(side_effect=requests.ConnectionError("offline"))

        with self.assertRaisesRegex(WorkerAPIError, "API interna indisponível"):
            client.request("GET", "/api/jobs/status")


class FacilAdapterLoginTests(unittest.IsolatedAsyncioTestCase):
    async def test_login_preserves_captcha_failures_for_canonical_classification(self) -> None:
        page = AsyncMock()
        with patch(
            "workers.adapters.facil.resolve_captcha",
            new=AsyncMock(side_effect=CaptchaError("sem saldo")),
        ):
            with self.assertRaisesRegex(CaptchaError, "sem saldo"):
                await _login_without_side_effects(page, "https://facil", "user", "pass")

    async def test_login_uses_positive_portal_confirmation(self) -> None:
        page = AsyncMock()
        with (
            patch(
                "workers.adapters.facil.resolve_captcha",
                new=AsyncMock(return_value="12345"),
            ),
            patch(
                "workers.adapters.facil.check_login_success",
                new=AsyncMock(return_value=True),
            ),
        ):
            self.assertTrue(
                await _login_without_side_effects(
                    page, "https://facil", "user", "pass"
                )
            )


class GenericWorkerTests(unittest.TestCase):
    ITEM = {"item_id": 11, "cpf": "01234567890", "registration": "ABC1"}

    def test_found_completes_item_and_always_releases_credential(self) -> None:
        api = FakeAPI(claims=[[self.ITEM], []])
        outcome = ExecutionOutcome.found(
            requested={"cpf": "01234567890"},
            confirmed={"cpf": "01234567890"},
            raw={"Status_Robo": "Sucesso"},
        )
        worker, session = make_worker(api, outcome)
        self.assertTrue(worker.run_once())

        completion = api.calls_for("/api/workers/items/complete")[0]["json"]
        self.assertEqual("completed", completion["status"])
        self.assertEqual("found", completion["result_data"]["outcome"])
        self.assertTrue(api.calls_for("/api/workers/heartbeat"))
        self.assertTrue(api.calls_for("/api/workers/release"))
        self.assertTrue(session.closed)

    def test_not_found_is_a_successful_terminal_result(self) -> None:
        api = FakeAPI(claims=[[self.ITEM], []])
        worker, _ = make_worker(
            api,
            ExecutionOutcome.not_found(requested={"cpf": "01234567890"}),
        )
        worker.run_once()
        completion = api.calls_for("/api/workers/items/complete")[0]["json"]
        self.assertEqual("completed", completion["status"])
        self.assertEqual("not_found", completion["result_data"]["outcome"])

    def test_retryable_error_is_always_delegated_to_backend_retry_policy(self) -> None:
        api = FakeAPI()
        worker, _ = make_worker(
            api,
            ExecutionOutcome.error(
                OutcomeKind.RETRYABLE_ERROR,
                requested={"cpf": "01234567890"},
                code="portal_timeout",
                message="Resposta lenta.",
            ),
        )
        item = WorkItem.from_api(self.ITEM)
        outcome = ExecutionOutcome.error(
            OutcomeKind.RETRYABLE_ERROR,
            requested=item.requested,
            code="portal_timeout",
            message="Resposta lenta.",
        )
        self.assertEqual("requeued", worker._apply_outcome(item, 3, outcome))
        self.assertEqual("requeued", worker._apply_outcome(item, 3, outcome))
        self.assertEqual("requeued", worker._apply_outcome(item, 3, outcome))
        self.assertEqual(3, len(api.calls_for("/api/workers/items/requeue")))
        self.assertFalse(api.calls_for("/api/workers/items/complete"))

    def test_integration_outage_requeues_and_cools_down_credential(self) -> None:
        api = FakeAPI()
        worker, _ = make_worker(
            api,
            ExecutionOutcome.error(OutcomeKind.INTEGRATION_UNAVAILABLE),
        )
        item = WorkItem.from_api(self.ITEM)
        outcome = ExecutionOutcome.error(
            OutcomeKind.INTEGRATION_UNAVAILABLE,
            requested=item.requested,
            code="captcha_unavailable",
            message="2Captcha sem saldo.",
            retry_after_seconds=1800,
            end_session=True,
        )
        self.assertEqual("end_session", worker._apply_outcome(item, 3, outcome))
        report = api.calls_for("/api/workers/credentials/report")[0]["json"]
        self.assertEqual("portal_unavailable", report["outcome"])
        self.assertEqual(1800, report["cooldown_seconds"])

    def test_credential_error_requeues_and_invalidates_only_that_credential(self) -> None:
        api = FakeAPI()
        worker, _ = make_worker(
            api,
            ExecutionOutcome.error(OutcomeKind.CREDENTIAL_ERROR),
        )
        item = WorkItem.from_api(self.ITEM)
        outcome = ExecutionOutcome.error(
            OutcomeKind.CREDENTIAL_ERROR,
            requested=item.requested,
            message="Acesso recusado.",
            end_session=True,
        )
        worker._apply_outcome(item, 3, outcome)
        report = api.calls_for("/api/workers/credentials/report")[0]["json"]
        self.assertEqual("invalid_credentials", report["outcome"])

    def test_acquire_conflict_does_not_open_portal(self) -> None:
        class ConflictAPI(FakeAPI):
            def request(self, method: str, path: str, **kwargs):
                if path == "/api/workers/credentials/acquire":
                    raise WorkerAPIConflict("sem credencial")
                return super().request(method, path, **kwargs)

        api = ConflictAPI()
        worker, session = make_worker(
            api,
            ExecutionOutcome.not_found(requested={}),
        )
        self.assertFalse(worker.run_once())
        self.assertFalse(session.closed)

    def test_backend_is_the_only_authority_for_job_executability(self) -> None:
        api = FakeAPI()
        worker, _ = make_worker(api, ExecutionOutcome.not_found(requested={}))
        api.status["running"][0]["executable"] = False
        self.assertFalse(worker.run_once())
        self.assertTrue(api.calls_for("/api/jobs/status"))
        self.assertFalse(api.calls_for("/api/workers/credentials/acquire"))


if __name__ == "__main__":
    unittest.main()

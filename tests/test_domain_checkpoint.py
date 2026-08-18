"""Regressões das regras de domínio introduzidas no checkpoint 2026-08-18."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from machine_admin.config import Settings
from machine_admin.datasets import import_dataset, normalize_cpf
from machine_admin.models import (
    Base,
    Dataset,
    DatasetRecord,
    Municipality,
    Platform,
)
from machine_admin.services import (
    create_portal_credential,
    sync_catalog,
    update_portal_credential,
)
from services.registry import MUNICIPALITIES


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


class FakeSession:
    def __init__(self, objects: list[object] | None = None) -> None:
        self.objects: dict[tuple[type[object], object], object] = {}
        self.records: list[object] = []
        self.commits = 0
        for value in objects or []:
            self._remember(value)

    def _remember(self, value: object) -> None:
        key = getattr(value, "slug", None)
        if key is not None:
            self.objects[(type(value), key)] = value

    def get(self, model: type[object], key: object) -> object | None:
        return self.objects.get((model, key))

    def add(self, value: object) -> None:
        if isinstance(value, Dataset) and value.id is None:
            value.id = 1
        self.records.append(value)
        self._remember(value)

    def add_all(self, values: list[object]) -> None:
        for value in values:
            self.add(value)

    def flush(self) -> None:
        return None

    def commit(self) -> None:
        self.commits += 1


class DomainCheckpointTests(unittest.TestCase):
    def test_cpf_uses_check_digits_and_recovers_excel_leading_zeroes(self) -> None:
        self.assertEqual("52998224725", normalize_cpf("529.982.247-25"))
        self.assertEqual("00123456797", normalize_cpf("123456797"))
        self.assertIsNone(normalize_cpf("52998224724"))
        self.assertIsNone(normalize_cpf("11111111111"))
        self.assertIsNone(normalize_cpf("invalido"))

    def test_default_duplicate_policy_keeps_first_logical_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = FakeSession()
            dataset = import_dataset(
                session,
                settings_for(Path(directory)),
                municipality_slug="paulista",
                filename="consulta_agosto.csv",
                payload=(
                    b"CPF,MATRICULA\n"
                    b"52998224725,ABC\n"
                    b"529.982.247-25,ABC\n"
                    b"52998224725,DEF\n"
                ),
                uploaded_by_id=1,
            )

            records = [value for value in session.records if isinstance(value, DatasetRecord)]
            self.assertEqual(2, dataset.row_count)
            self.assertEqual(2, len(records))
            self.assertEqual("consulta_agosto", dataset.display_name)
            self.assertEqual("keep_first", dataset.duplicate_policy)
            self.assertEqual(1, dataset.metadata_json["duplicate_row_count"])
            self.assertIn("duplicata(s) ignorada(s)", dataset.error_message or "")

    def test_duplicate_policy_can_reject_or_keep_all(self) -> None:
        payload = b"CPF,MATRICULA\n52998224725,ABC\n52998224725,ABC\n"
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "duplicado"):
                import_dataset(
                    FakeSession(),
                    settings_for(Path(directory)),
                    municipality_slug="paulista",
                    filename="rejeitada.csv",
                    payload=payload,
                    uploaded_by_id=1,
                    duplicate_policy="reject",
                )

        with tempfile.TemporaryDirectory() as directory:
            dataset = import_dataset(
                FakeSession(),
                settings_for(Path(directory)),
                municipality_slug="paulista",
                filename="historica.csv",
                payload=payload,
                uploaded_by_id=1,
                duplicate_policy="keep_all",
                display_name="Base histórica",
                metadata={"source": "legacy"},
            )
            self.assertEqual(2, dataset.row_count)
            self.assertEqual("Base histórica", dataset.display_name)
            self.assertEqual("legacy", dataset.metadata_json["source"])

    def test_agreement_input_schema_controls_registration_and_duplicate_key(self) -> None:
        paulista = Municipality(
            slug="paulista",
            name="Paulista",
            platform_slug="facil",
            max_workers=1,
            enabled=True,
            operational_status="ready",
            timezone="America/Fortaleza",
            input_schema=MUNICIPALITIES["paulista"].input_schema,
            settings_json={},
        )
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "MATRICULA"):
                import_dataset(
                    FakeSession([paulista]),
                    settings_for(Path(directory)),
                    municipality_slug="paulista",
                    filename="sem_matricula.csv",
                    payload=b"CPF\n52998224725\n",
                    uploaded_by_id=1,
                )

        boa_vista = Municipality(
            slug="boa-vista",
            name="Boa Vista",
            platform_slug="rf1",
            max_workers=1,
            enabled=True,
            operational_status="ready",
            timezone="America/Boa_Vista",
            input_schema=MUNICIPALITIES["boa-vista"].input_schema,
            settings_json={},
        )
        with tempfile.TemporaryDirectory() as directory:
            dataset = import_dataset(
                FakeSession([boa_vista]),
                settings_for(Path(directory)),
                municipality_slug="boa-vista",
                filename="duplicada.csv",
                payload=(
                    b"CPF,MATRICULA\n"
                    b"52998224725,ABC\n"
                    b"52998224725,DEF\n"
                ),
                uploaded_by_id=1,
            )
            self.assertEqual(1, dataset.row_count)
            self.assertEqual(["cpf"], dataset.metadata_json["duplicate_key"])

    def test_catalog_seeds_missing_rows_without_overwriting_existing_configuration(self) -> None:
        existing_platform = Platform(
            slug="rf1",
            name="Nome operacional",
            runner="rf1-custom",
            start_hour=1,
            end_hour=2,
            enabled=False,
        )
        existing_municipality = Municipality(
            slug="boa-vista",
            name="Boa Vista customizada",
            platform_slug="rf1",
            login_url="https://custom.invalid/login",
            query_url="https://custom.invalid/query",
            max_workers=1,
            enabled=False,
            operational_status="paused",
            timezone="UTC",
            input_schema={"version": 99},
            schedule_policy={"weekdays": [2], "start_hour": 3, "end_hour": 4},
            adapter_version="rf1.custom",
            settings_json={},
        )
        session = FakeSession([existing_platform, existing_municipality])

        sync_catalog(session)

        self.assertEqual("Nome operacional", existing_platform.name)
        self.assertEqual("rf1-custom", existing_platform.runner)
        self.assertEqual("paused", existing_municipality.operational_status)
        self.assertEqual("https://custom.invalid/login", existing_municipality.login_url)
        self.assertEqual([2], existing_municipality.schedule_policy["weekdays"])
        seeded = session.get(Municipality, "gov-am")
        self.assertIsNotNone(seeded)
        self.assertEqual("ready", seeded.operational_status)
        self.assertEqual(1, session.commits)

    def test_portal_profile_is_optional_and_legacy_column_remains_compatible(self) -> None:
        municipality = Municipality(
            slug="gov-am",
            name="GOV AM",
            platform_slug="facil",
            max_workers=1,
            enabled=True,
            operational_status="ready",
            timezone="America/Manaus",
            input_schema={},
            settings_json={},
        )
        session = FakeSession([municipality])
        with tempfile.TemporaryDirectory() as directory:
            credential = create_portal_credential(
                session,
                settings_for(Path(directory)),
                municipality_slug="gov-am",
                label="Conta principal",
                username="operador",
                password="senha-de-portal",
            )
        self.assertIsNone(credential.portal_profile)
        self.assertIsNone(credential.consignataria)

    def test_corrected_access_data_reactivates_an_invalid_credential(self) -> None:
        municipality = Municipality(
            slug="gov-am",
            name="GOV AM",
            platform_slug="facil",
            max_workers=1,
            enabled=True,
            operational_status="ready",
            timezone="America/Manaus",
            input_schema={},
            settings_json={},
        )
        session = FakeSession([municipality])
        with tempfile.TemporaryDirectory() as directory:
            config = settings_for(Path(directory))
            credential = create_portal_credential(
                session,
                config,
                municipality_slug="gov-am",
                label="Conta principal",
                username="operador-antigo",
                password="senha-antiga",
            )
            credential.status = "invalid"
            credential.failure_count = 3
            credential.last_error = "Senha recusada"

            update_portal_credential(
                session,
                config,
                credential=credential,
                label="Conta principal",
                username="operador-correto",
                password="senha-correta",
            )

        self.assertEqual("active", credential.status)
        self.assertEqual(0, credential.failure_count)
        self.assertIsNone(credential.last_error)
        self.assertEqual("operador-correto", credential.portal_username)

    def test_portal_credential_field_lengths_are_validated_before_database(self) -> None:
        municipality = Municipality(
            slug="gov-am",
            name="GOV AM",
            platform_slug="facil",
            max_workers=1,
            enabled=True,
            operational_status="ready",
            timezone="America/Manaus",
            input_schema={},
            settings_json={},
        )
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "120"):
                create_portal_credential(
                    FakeSession([municipality]),
                    settings_for(Path(directory)),
                    municipality_slug="gov-am",
                    label="x" * 121,
                    username="operador",
                    password="senha",
                )

    def test_registry_only_marks_homologated_agreements_ready(self) -> None:
        ready = {
            slug
            for slug, definition in MUNICIPALITIES.items()
            if definition.operational_status == "ready"
        }
        self.assertEqual({"boa-vista", "gov-am", "paulista"}, ready)
        self.assertEqual("testing", MUNICIPALITIES["itabuna"].operational_status)
        self.assertEqual("draft", MUNICIPALITIES["fortaleza"].operational_status)
        self.assertEqual(
            [0, 1, 2, 3, 4, 5, 6],
            MUNICIPALITIES["boa-vista"].schedule_policy["weekdays"],
        )
        self.assertEqual(
            {"weekdays": [0, 1, 2, 3, 4], "start_hour": None, "end_hour": None},
            MUNICIPALITIES["gov-am"].schedule_policy,
        )

    def test_new_operational_tables_and_columns_are_declared(self) -> None:
        self.assertEqual(17, len(Base.metadata.tables))
        self.assertIn("job_item_attempts", Base.metadata.tables)
        self.assertIn("worker_heartbeats", Base.metadata.tables)
        self.assertIn("notification_outbox", Base.metadata.tables)
        self.assertIn("outcome", Base.metadata.tables["job_items"].c)
        self.assertIn("found_items", Base.metadata.tables["automation_jobs"].c)


if __name__ == "__main__":
    unittest.main()

"""Regressões das regras de domínio introduzidas no checkpoint 2026-08-18."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from machine_admin.config import Settings
from machine_admin.datasets import import_dataset, normalize_cpf
from machine_admin.models import (
    Base,
    CredentialLease,
    Dataset,
    DatasetRecord,
    Municipality,
    Platform,
    PortalCredential,
)
from machine_admin.services import (
    create_portal_credential,
    sync_catalog,
    update_portal_credential,
)
from services.registry import MUNICIPALITIES
from tests.dataset_test_support import DatasetImportQueries


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


class FakeSession(DatasetImportQueries):
    def __init__(self, objects: list[object] | None = None) -> None:
        self.objects: dict[tuple[type[object], object], object] = {}
        self.records: list[object] = []
        self.commits = 0
        self.refreshes = []
        self.active_lease_credential_id = None
        for value in objects or []:
            self._remember(value)

    def _remember(self, value: object) -> None:
        key = getattr(value, "slug", None)
        if key is not None:
            self.objects[(type(value), key)] = value

    def get(self, model: type[object], key: object) -> object | None:
        return self.objects.get((model, key))

    def add(self, value: object) -> None:
        self.assign_record_id(value)
        self.records.append(value)
        self._remember(value)

    def add_all(self, values: list[object]) -> None:
        for value in values:
            self.add(value)

    def flush(self) -> None:
        return None

    def refresh(self, value, *, with_for_update=False):
        self.refreshes.append((value, with_for_update))

    def scalar(self, statement):
        if statement.column_descriptions[0].get("entity") is CredentialLease:
            return self.active_lease_credential_id
        return super().scalar(statement)

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
            session = FakeSession([Municipality(slug="paulista", name="Paulista",
                input_schema=MUNICIPALITIES["paulista"].input_schema)])
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
            self.assertIn("repetida(s) no arquivo foram ignoradas", dataset.error_message or "")

    def test_legacy_duplicate_policy_cannot_bypass_add_only_semantics(self) -> None:
        payload = b"CPF,MATRICULA\n52998224725,ABC\n52998224725,ABC\n"
        for legacy_policy in ("reject", "keep_all"):
            with self.subTest(policy=legacy_policy), tempfile.TemporaryDirectory() as directory:
                session = FakeSession([Municipality(slug="paulista", name="Paulista",
                    input_schema=MUNICIPALITIES["paulista"].input_schema)])
                dataset = import_dataset(
                    session,
                    settings_for(Path(directory)),
                    municipality_slug="paulista",
                    filename="historica.csv",
                    payload=payload,
                    uploaded_by_id=1,
                    duplicate_policy=legacy_policy,
                    display_name="Base histórica",
                    metadata={"source": "legacy"},
                )
                self.assertEqual(1, dataset.row_count)
                self.assertEqual(1, len([row for row in session.records if isinstance(row, DatasetRecord)]))
                self.assertEqual("keep_first", dataset.duplicate_policy)
                self.assertEqual(1, dataset.metadata_json["duplicate_row_count"])
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
        self.assertEqual([(credential, True)], session.refreshes)
        self.assertEqual(0, credential.login_failure_count)

    def test_account_in_use_cannot_be_edited_before_logout(self) -> None:
        session = FakeSession()
        session.active_lease_credential_id = 22
        credential = PortalCredential(id=22, label="Original", status="active")
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "está em uso"):
                update_portal_credential(session, settings_for(Path(directory)),
                    credential=credential, label="Edited", username="changed", password="changed")
        self.assertEqual("Original", credential.label)
        self.assertEqual([(credential, True)], session.refreshes)

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
        self.assertEqual("testing", MUNICIPALITIES["fortaleza"].operational_status)
        self.assertEqual(
            ["cpf", "registration"],
            MUNICIPALITIES["fortaleza"].input_schema["required"],
        )
        self.assertEqual(
            ["cpf", "registration"],
            MUNICIPALITIES["fortaleza"].input_schema["deduplication_key"],
        )
        self.assertEqual(
            [0, 1, 2, 3, 4, 5, 6],
            MUNICIPALITIES["boa-vista"].schedule_policy["weekdays"],
        )
        self.assertEqual(
            {"weekdays": [0, 1, 2, 3, 4], "start_hour": None, "end_hour": None},
            MUNICIPALITIES["gov-am"].schedule_policy,
        )

    def test_fortaleza_catalog_transition_is_persisted_by_migration(self) -> None:
        migration = (
            Path(__file__).parents[1]
            / "migrations"
            / "versions"
            / "20260829_0007_safeconsig_fortaleza.py"
        ).read_text()

        self.assertIn("down_revision = \"20260818_0006\"", migration)
        self.assertIn("adapter_version = 'safeconsig.v1'", migration)
        self.assertIn("jsonb_build_array('cpf', 'registration')", migration)
        self.assertIn("operational_status = 'draft' THEN 'testing'", migration)

    def test_new_operational_tables_and_columns_are_declared(self) -> None:
        self.assertEqual(25, len(Base.metadata.tables))
        self.assertIn("dataset_memberships", Base.metadata.tables)
        self.assertIn("dataset_type", Base.metadata.tables["datasets"].c)
        self.assertIn("job_item_attempts", Base.metadata.tables)
        self.assertIn("worker_heartbeats", Base.metadata.tables)
        self.assertIn("notification_outbox", Base.metadata.tables)
        self.assertIn("outcome", Base.metadata.tables["job_items"].c)
        self.assertIn("found_items", Base.metadata.tables["automation_jobs"].c)
        for table in ("operational_blocks", "portal_access_checks", "consultation_schedules",
                      "schedule_occurrences", "job_requests", "export_artifacts"):
            self.assertIn(table, Base.metadata.tables)
        self.assertIn("lease_token", Base.metadata.tables["job_items"].c)
        self.assertIn("retry_count", Base.metadata.tables["job_items"].c)
        self.assertIn("lease_token", Base.metadata.tables["credential_leases"].c)
        self.assertIn("selected_credential_ids", Base.metadata.tables["automation_jobs"].c)


if __name__ == "__main__":
    unittest.main()

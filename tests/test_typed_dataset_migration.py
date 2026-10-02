"""Upgrade populated legacy schemas without rewriting historical bases or jobs."""

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
import secrets
import unittest

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import MetaData, Table, create_engine, select, text
from sqlalchemy.engine import make_url

from machine_admin.security import SecretCipher


@unittest.skipUnless(os.getenv("MACHINE_TEST_DATABASE_URL"), "isolated PostgreSQL URL not configured")
class TypedDatasetMigration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        url = make_url(os.environ["MACHINE_TEST_DATABASE_URL"])
        if url.get_backend_name() != "postgresql" or not (url.database or "").startswith("machine_acceptance_"):
            raise RuntimeError("Refusing non-isolated database")
        cls.engine = create_engine(url)
        cls.schema = "migration_typed_" + secrets.token_hex(8)
        with cls.engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{cls.schema}"'))
        cls.addClassCleanup(cls.cleanup_schema)
        config = Config()
        config.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "migrations"))
        scripts = ScriptDirectory.from_config(config)
        cls.target_revision = scripts.get_revision("head")
        with cls.engine.begin() as connection:
            connection.execute(text(f'SET LOCAL search_path TO "{cls.schema}"'))
            with Operations.context(MigrationContext.configure(connection)):
                for revision in reversed(list(scripts.iterate_revisions("20260930_0009", "base"))):
                    revision.module.upgrade()
            cls.seed_legacy(connection)
            cls.before = cls.snapshot(connection)
            with Operations.context(MigrationContext.configure(connection)):
                for revision in reversed(list(scripts.iterate_revisions("head", "20260930_0009"))):
                    revision.module.upgrade()

    @classmethod
    def cleanup_schema(cls):
        with cls.engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{cls.schema}" CASCADE'))
        cls.engine.dispose()

    @classmethod
    def snapshot(cls, connection):
        tables = ("datasets", "dataset_records", "automation_jobs", "job_items", "consultation_schedules")
        return {name: [dict(row) for row in connection.execute(text(f"SELECT * FROM {name} ORDER BY id")).mappings()]
                for name in tables}

    @classmethod
    def seed_legacy(cls, connection):
        metadata = MetaData()
        tables = {name: Table(name, metadata, autoload_with=connection) for name in (
            "platforms", "municipalities", "datasets", "dataset_records",
            "automation_jobs", "job_items", "consultation_schedules",
        )}
        connection.execute(tables["platforms"].insert().values(slug="legacy", name="Legacy", runner="rf1"))
        connection.execute(tables["municipalities"].insert().values(
            slug="legacy", name="Legacy", platform_slug="legacy",
            input_schema={"deduplication_key": ["cpf"]},
        ))
        cls.dataset_ids = []
        cls.record_ids = []
        cls.cipher = SecretCipher(b"k" * 32)
        for index, identities in enumerate(((1, 1, 2), (2, 3), (4,))):
            dataset_id = connection.scalar(tables["datasets"].insert().values(
                municipality_slug="legacy", original_filename=f"legacy-{index}.csv",
                display_name=f"Legacy {index}", storage_path=f"/synthetic/{index}.enc",
                sha256=f"{index:064d}", row_count=len(identities),
                status="archived" if index == 2 else "ready", duplicate_policy="keep_all",
            ).returning(tables["datasets"].c.id))
            cls.dataset_ids.append(dataset_id)
            ids = []
            for row_number, identity in enumerate(identities, start=2):
                context = f"legacy-{index}-{row_number}"
                source = {"CPF": f"synthetic-{identity}", "SOURCE": f"base-{index}-{row_number}"}
                ids.append(connection.scalar(tables["dataset_records"].insert().values(
                    dataset_id=dataset_id, row_number=row_number, encryption_context=context,
                    cpf_ciphertext=cls.cipher.encrypt(source["CPF"], context=f"record:{context}:cpf"),
                    cpf_fingerprint=f"{identity:064d}", cpf_last4=f"{identity:04d}",
                    registration=" A ",
                    source_ciphertext=cls.cipher.encrypt(json.dumps(source), context=f"record:{context}:source"),
                    source_data={"columns": list(source)},
                ).returning(tables["dataset_records"].c.id)))
            cls.record_ids.append(ids)
        cls.job_id = connection.scalar(tables["automation_jobs"].insert().values(
            municipality_slug="legacy", dataset_id=cls.dataset_ids[1],
            status="paused", total_items=2,
        ).returning(tables["automation_jobs"].c.id))
        connection.execute(tables["job_items"].insert(), [
            {"job_id": cls.job_id, "dataset_record_id": identity} for identity in cls.record_ids[1]
        ])
        connection.execute(tables["consultation_schedules"].insert().values(
            name="Legacy daily", dataset_id=cls.dataset_ids[1], cron_expression="0 9 * * *",
            timezone="America/Fortaleza", enabled=True, next_run_at=datetime.now(UTC) + timedelta(days=1),
        ))

    def test_existing_bases_jobs_records_and_schedules_are_unchanged(self):
        with self.engine.begin() as connection:
            connection.execute(text(f'SET LOCAL search_path TO "{self.schema}"'))
            after = self.snapshot(connection)
            for dataset in after["datasets"]:
                self.assertIsNone(dataset.pop("dataset_type"))
            self.assertEqual(self.before, after)

    def test_every_legacy_record_gets_membership_even_repeated_cpf(self):
        with self.engine.begin() as connection:
            connection.execute(text(f'SET LOCAL search_path TO "{self.schema}"'))
            rows = list(connection.execute(text(
                "SELECT dataset_id, dataset_record_id, identity_key FROM dataset_memberships ORDER BY id"
            )).mappings())
            self.assertEqual(6, len(rows))
            for dataset_id, expected_ids in zip(self.dataset_ids, self.record_ids):
                members = [row for row in rows if row["dataset_id"] == dataset_id]
                self.assertEqual(expected_ids, [row["dataset_record_id"] for row in members])
                self.assertEqual(len(expected_ids), len({row["identity_key"] for row in members}))

    def test_source_ciphertext_remains_decryptable_in_its_original_context(self):
        with self.engine.begin() as connection:
            connection.execute(text(f'SET LOCAL search_path TO "{self.schema}"'))
            records = Table("dataset_records", MetaData(), autoload_with=connection)
            for record in connection.execute(select(records)).mappings():
                context = record["encryption_context"]
                source = json.loads(self.cipher.decrypt(record["source_ciphertext"], context=f"record:{context}:source"))
                cpf = self.cipher.decrypt(record["cpf_ciphertext"], context=f"record:{context}:cpf")
                self.assertEqual(source["CPF"], cpf)

    def test_legacy_only_catalog_can_downgrade_without_losing_original_data(self):
        with self.engine.begin() as connection:
            connection.execute(text(f'SET LOCAL search_path TO "{self.schema}"'))
            transaction = connection.begin_nested()
            try:
                with Operations.context(MigrationContext.configure(connection)):
                    self.target_revision.module.downgrade()
                self.assertEqual(self.before, self.snapshot(connection))
            finally:
                transaction.rollback()

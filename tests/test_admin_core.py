"""Regressões da base administrativa sem depender de serviços externos."""

from __future__ import annotations

import tempfile
import unittest
import json
from inspect import signature
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from cryptography.exceptions import InvalidTag
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from machine_admin.config import Settings
from machine_admin.datasets import import_dataset
from machine_admin.datasets import normalise_custom_columns
from machine_admin.db import get_db
from machine_admin.models import Base, Dataset, DatasetRecord
from machine_admin.queue import (
    credential_candidate_statement,
    job_item_claim_statement,
)
from machine_admin.security import (
    SecretCipher,
    fingerprint_identifier,
    generate_api_token,
    hash_password,
    token_matches,
    verify_password,
)
from machine_admin.web import ApiPrincipal, create_app


def settings_for(storage_dir: Path) -> Settings:
    return Settings(
        database_url="postgresql://machine:test@localhost/machine",
        session_secret="s" * 48,
        master_key=b"k" * 32,
        cookie_secure=False,
        allowed_hosts=("testserver", "localhost"),
        storage_dir=storage_dir,
        max_upload_bytes=1024 * 1024,
        bootstrap_admin_email=None,
        bootstrap_admin_password=None,
    )


class FakeImportSession:
    def __init__(self) -> None:
        self.records: list[object] = []

    def add(self, value: object) -> None:
        if isinstance(value, Dataset) and value.id is None:
            value.id = 1
        self.records.append(value)

    def add_all(self, values: list[object]) -> None:
        self.records.extend(values)

    def flush(self) -> None:
        return None


class FakeWebSession:
    def get(self, *_: object) -> None:
        return None


class AdminCoreTests(unittest.TestCase):
    def test_secret_cipher_binds_ciphertext_to_context(self) -> None:
        cipher = SecretCipher(b"a" * 32)
        encrypted = cipher.encrypt("valor-sensível", context="portal:one")

        self.assertNotIn(b"valor", encrypted)
        self.assertEqual(
            "valor-sensível", cipher.decrypt(encrypted, context="portal:one")
        )
        with self.assertRaises(InvalidTag):
            cipher.decrypt(encrypted, context="portal:two")

    def test_passwords_tokens_and_fingerprints_are_one_way(self) -> None:
        password_hash = hash_password("uma-senha-bem-forte")
        self.assertTrue(verify_password(password_hash, "uma-senha-bem-forte"))
        self.assertFalse(verify_password(password_hash, "senha-incorreta"))

        raw_token, prefix, stored_hash = generate_api_token()
        self.assertTrue(raw_token.startswith(f"mc_{prefix}_"))
        self.assertNotIn(raw_token, stored_hash)
        self.assertTrue(token_matches(stored_hash, raw_token))
        self.assertNotEqual(
            fingerprint_identifier(b"a" * 32, "01234567890"),
            fingerprint_identifier(b"a" * 32, "01234567891"),
        )

    def test_queue_claims_compile_with_skip_locked(self) -> None:
        now = datetime.now(UTC)
        statements = (
            credential_candidate_statement(municipality_slug="boa-vista", now=now),
            job_item_claim_statement(job_id=1, now=now, batch_size=10),
        )
        for statement in statements:
            sql = str(statement.compile(dialect=postgresql.dialect()))
            self.assertIn("FOR UPDATE SKIP LOCKED", sql)

    def test_dataset_is_encrypted_and_invalid_blob_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Path(directory)
            config = settings_for(storage)
            session = FakeImportSession()
            dataset = import_dataset(
                session,
                config,
                municipality_slug="boa-vista",
                filename="base.csv",
                payload=b"CPF,MATRICULA\n52998224725,ABC\n",
                uploaded_by_id=1,
            )
            encrypted_file = Path(dataset.storage_path)
            self.assertTrue(encrypted_file.exists())
            self.assertNotIn(b"52998224725", encrypted_file.read_bytes())
            record = next(value for value in session.records if isinstance(value, DatasetRecord))
            cpf = SecretCipher(config.master_key).decrypt(
                record.cpf_ciphertext,
                context=f"record:{record.encryption_context}:cpf",
            )
            self.assertEqual("52998224725", cpf)

            invalid_session = FakeImportSession()
            with self.assertRaises(ValueError):
                import_dataset(
                    invalid_session,
                    config,
                    municipality_slug="boa-vista",
                    filename="invalida.csv",
                    payload=b"CPF\ninvalido\n",
                    uploaded_by_id=1,
                )
            self.assertEqual(1, len(list(storage.rglob("*.enc"))))

    def test_dataset_import_keeps_valid_rows_when_some_cpfs_are_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            dataset = import_dataset(
                FakeImportSession(),
                settings_for(Path(directory)),
                municipality_slug="boa-vista",
                filename="mixed.csv",
                payload=b"CPF\n52998224725\ninvalido\n",
                uploaded_by_id=1,
            )
            self.assertEqual(1, dataset.row_count)
            self.assertIn("linha(s) ignorada(s)", dataset.error_message or "")

    def test_custom_columns_are_normalized_and_persisted_in_records(self) -> None:
        self.assertEqual(
            ["Banco", "Consultor"],
            normalise_custom_columns("Banco, Consultor\nBanco"),
        )
        with tempfile.TemporaryDirectory() as directory:
            session = FakeImportSession()
            dataset = import_dataset(
                session,
                settings_for(Path(directory)),
                municipality_slug="itabuna",
                filename="base.csv",
                payload=b"CPF,MATRICULA\n52998224725,ABC\n",
                uploaded_by_id=1,
                custom_columns="Banco, Consultor",
            )
            self.assertEqual(["Banco", "Consultor"], dataset.custom_columns)
            record = next(value for value in session.records if isinstance(value, DatasetRecord))
            self.assertEqual(
                ["CPF", "MATRICULA", "Banco", "Consultor"],
                record.source_data["columns"],
            )

    def test_dataset_requires_cpf_as_first_column_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            dataset = import_dataset(
                FakeImportSession(),
                settings_for(Path(directory)),
                municipality_slug="itabuna",
                filename="base.csv",
                payload=b"CPF,BANCO\n52998224725,Exemplo\n",
                uploaded_by_id=1,
            )
            self.assertEqual(1, dataset.row_count)
            with self.assertRaisesRegex(ValueError, "primeira coluna"):
                import_dataset(
                    FakeImportSession(),
                    settings_for(Path(directory)),
                    municipality_slug="itabuna",
                    filename="base.csv",
                    payload=b"MATRICULA,CPF\nABC,52998224725\n",
                    uploaded_by_id=1,
                )

    def test_app_registers_admin_and_worker_routes_and_renders_login(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(settings_for(Path(directory)))

            def fake_db():
                yield FakeWebSession()

            app.dependency_overrides[get_db] = fake_db
            paths = {route.path for route in app.routes}
            self.assertIn("/admin/credentials", paths)
            self.assertIn("/admin/credentials/{credential_id}/edit", paths)
            self.assertIn("/admin/datasets", paths)
            self.assertIn("/admin/logs", paths)
            self.assertIn("/admin/agreements", paths)
            self.assertIn("/admin/notifications", paths)
            self.assertIn("/api/workers/items/claim", paths)
            self.assertIn("/api/runtime/secrets/{secret_key}", paths)
            upload_route = next(route for route in app.routes if route.path == "/admin/datasets" and "POST" in route.methods)
            self.assertNotIn("custom_columns", signature(upload_route.endpoint).parameters)
            self.assertIn("/admin/datasets/{dataset_id}/jobs", paths)
            self.assertEqual(17, len(Base.metadata.tables))

            response = TestClient(app).get("/login")
            self.assertEqual(200, response.status_code)
            self.assertIn("Machine Admin", response.text)

    def test_runtime_secret_endpoint_has_a_closed_allowlist_and_no_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(settings_for(Path(directory)))
        endpoint = next(
            route.endpoint
            for route in app.routes
            if route.path == "/api/runtime/secrets/{secret_key}"
        )
        with patch("machine_admin.web.get_runtime_secret", return_value="rotated"):
            response = endpoint(
                "TWOCAPTCHA_API_KEY",
                ApiPrincipal("worker", frozenset({"workers:execute"})),
            )
        self.assertEqual(
            {"key": "TWOCAPTCHA_API_KEY", "value": "rotated"},
            json.loads(response.body),
        )
        self.assertIn("no-store", response.headers["cache-control"])

        with patch("machine_admin.web.get_runtime_secret", return_value="proxy"):
            response = endpoint(
                "SAFECONSIG_HTTP_PROXY",
                ApiPrincipal("worker", frozenset({"workers:execute"})),
            )
        self.assertEqual(
            {"key": "SAFECONSIG_HTTP_PROXY", "value": "proxy"},
            json.loads(response.body),
        )

        with self.assertRaises(Exception) as caught:
            endpoint(
                "APP_MASTER_KEY",
                ApiPrincipal("worker", frozenset({"workers:execute"})),
            )
        self.assertEqual(404, getattr(caught.exception, "status_code", None))


if __name__ == "__main__":
    unittest.main()

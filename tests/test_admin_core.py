"""Regressões da base administrativa sem depender de serviços externos."""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from cryptography.exceptions import InvalidTag
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from machine_admin.config import Settings
from machine_admin.datasets import import_dataset
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
from machine_admin.web import create_app


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
                payload=b"CPF,MATRICULA\n01234567890,ABC\n",
                uploaded_by_id=1,
            )
            encrypted_file = Path(dataset.storage_path)
            self.assertTrue(encrypted_file.exists())
            self.assertNotIn(b"01234567890", encrypted_file.read_bytes())
            record = next(value for value in session.records if isinstance(value, DatasetRecord))
            cpf = SecretCipher(config.master_key).decrypt(
                record.cpf_ciphertext,
                context=f"record:{record.encryption_context}:cpf",
            )
            self.assertEqual("01234567890", cpf)

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

    def test_app_registers_admin_and_worker_routes_and_renders_login(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(settings_for(Path(directory)))

            def fake_db():
                yield FakeWebSession()

            app.dependency_overrides[get_db] = fake_db
            paths = {route.path for route in app.routes}
            self.assertIn("/admin/credentials", paths)
            self.assertIn("/admin/datasets", paths)
            self.assertIn("/api/workers/items/claim", paths)
            self.assertEqual(14, len(Base.metadata.tables))

            response = TestClient(app).get("/login")
            self.assertEqual(200, response.status_code)
            self.assertIn("Machine Admin", response.text)


if __name__ == "__main__":
    unittest.main()

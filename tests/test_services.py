"""Regression tests for service boundaries that do not need external services."""

from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import patch


# Keep the core scheduling tests runnable before optional runtime dependencies
# are installed. Production uses the real requests package from requirements.txt.
sys.modules.setdefault(
    "requests", types.SimpleNamespace(RequestException=Exception, Session=object)
)

from services.database import require_postgres_url
from services.registry import (
    MUNICIPALITIES,
    PLATFORMS,
    enabled_municipalities,
    runner_names,
)
from services.scheduling import is_within_window, platform_for
from services.telegram import TelegramClient, TelegramNotifier
from services.utils import mask_cpf


class _Response:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return {"ok": True, "result": {"id": 1}}


class _Session:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def post(self, *args: object, **kwargs: object) -> _Response:
        self.calls.append((args, kwargs))
        return _Response()


class ServiceTests(unittest.TestCase):
    def test_registry_is_the_single_consistent_source(self) -> None:
        self.assertNotIn("fenix", runner_names())
        self.assertIn("consiglog", runner_names())
        self.assertEqual("safeconsig", platform_for("maranguape"))
        for municipality in enabled_municipalities():
            self.assertIn(municipality.slug, MUNICIPALITIES)
            self.assertTrue(PLATFORMS[municipality.platform_slug].enabled)
            # Toda plataforma ativa precisa ser aceita pela política de horário.
            self.assertIsInstance(is_within_window(municipality.platform_slug), bool)

    def test_cpf_is_masked_in_logs(self) -> None:
        masked = mask_cpf("028.851.452-18")
        self.assertEqual("***.***.***-5218", masked)
        self.assertNotIn("02885145218", masked)

    def test_runtime_requires_postgresql(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(RuntimeError):
                require_postgres_url()
        with patch.dict(
            "os.environ",
            {"DATABASE_URL": "postgresql://machine:secret@localhost/machine"},
            clear=True,
        ):
            self.assertTrue(require_postgres_url().startswith("postgresql+psycopg://"))

    def test_telegram_client_uses_one_api_adapter(self) -> None:
        session = _Session()
        client = TelegramClient("test-token", session)

        self.assertEqual({"id": 1}, client.send_message(12, "ok"))
        self.assertEqual(12, session.calls[0][1]["json"]["chat_id"])

    def test_telegram_notifier_falls_back_to_allowed_user(self) -> None:
        with patch("services.telegram.get_runtime_secret", return_value="test-token"):
            with patch.dict(
                "os.environ", {"TELEGRAM_ALLOWED_USER_IDS": "42"}, clear=True
            ):
                notifier = TelegramNotifier.from_environment()
        self.assertTrue(notifier.enabled)
        self.assertEqual(42, notifier.chat_id)

if __name__ == "__main__":
    unittest.main()

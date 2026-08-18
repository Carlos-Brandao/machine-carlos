from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from machine_admin.models import ApiToken
from scripts.ensure_service_tokens import _is_usable


class ServiceTokenBootstrapTests(unittest.TestCase):
    def test_token_requires_active_lifetime_and_all_scopes(self) -> None:
        token = ApiToken(
            owner_id=1,
            name="workers",
            token_prefix="prefix",
            token_hash="hash",
            scopes=["jobs:read", "workers:execute"],
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
        self.assertTrue(_is_usable(token, ["workers:execute"]))
        self.assertFalse(_is_usable(token, ["jobs:write"]))

        token.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        self.assertFalse(_is_usable(token, ["workers:execute"]))

        token.expires_at = None
        token.revoked_at = datetime.now(UTC)
        self.assertFalse(_is_usable(token, ["workers:execute"]))


if __name__ == "__main__":
    unittest.main()

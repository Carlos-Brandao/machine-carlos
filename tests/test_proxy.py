from __future__ import annotations

import unittest

from services.proxy import HttpProxy, parse_http_proxy


class HttpProxyTests(unittest.TestCase):
    def test_legacy_authenticated_proxy_has_both_provider_shapes(self) -> None:
        proxy = parse_http_proxy("proxy.example:10000:worker:secret")

        self.assertEqual(
            {
                "server": "http://proxy.example:10000",
                "username": "worker",
                "password": "secret",
            },
            proxy.playwright_settings(),
        )
        self.assertEqual(
            "worker:secret@proxy.example:10000", proxy.twocaptcha_value()
        )
        self.assertNotIn("secret", repr(proxy))

    def test_http_url_decodes_credentials(self) -> None:
        proxy = parse_http_proxy(
            "http://worker%40pool:p%40ss@proxy.example:8080"
        )

        self.assertEqual("worker@pool", proxy.username)
        self.assertEqual("p@ss", proxy.password)

    def test_invalid_proxy_is_rejected_without_echoing_value(self) -> None:
        with self.assertRaises(ValueError) as raised:
            parse_http_proxy("https://private-value.example:443")

        self.assertNotIn("private-value", str(raised.exception))

    def test_unauthenticated_proxy_is_supported(self) -> None:
        proxy = HttpProxy("proxy.example", 3128)

        self.assertEqual(
            {"server": "http://proxy.example:3128"},
            proxy.playwright_settings(),
        )
        self.assertEqual("proxy.example:3128", proxy.twocaptcha_value())


if __name__ == "__main__":
    unittest.main()

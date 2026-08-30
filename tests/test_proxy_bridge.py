from __future__ import annotations

import socket
import threading
import unittest
from urllib.parse import urlsplit

from services.proxy import PortalProxy
from services.proxy_bridge import Socks5HttpBridge


def receive_exact(connection: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = connection.recv(size - len(data))
        if not chunk:
            raise RuntimeError("fake proxy closed")
        data.extend(chunk)
    return bytes(data)


class ProxyBridgeTests(unittest.TestCase):
    def test_http_connect_is_forwarded_through_authenticated_socks5(self) -> None:
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(5)
        state: dict[str, object] = {}

        def fake_proxy() -> None:
            try:
                connection, _address = listener.accept()
                with connection:
                    state["greeting"] = receive_exact(connection, 3)
                    connection.sendall(b"\x05\x02")
                    version, username_length = receive_exact(connection, 2)
                    username = receive_exact(connection, username_length)
                    password_length = receive_exact(connection, 1)[0]
                    password = receive_exact(connection, password_length)
                    state["auth"] = (version, username, password)
                    connection.sendall(b"\x01\x00")
                    state["connect_header"] = receive_exact(connection, 4)
                    host_length = receive_exact(connection, 1)[0]
                    state["target_host"] = receive_exact(
                        connection, host_length
                    ).decode()
                    state["target_port"] = int.from_bytes(
                        receive_exact(connection, 2), "big"
                    )
                    connection.sendall(
                        b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00"
                    )
                    while chunk := connection.recv(4096):
                        connection.sendall(chunk)
            except Exception as exc:  # pragma: no cover - surfaced below
                state["error"] = exc

        proxy_thread = threading.Thread(target=fake_proxy, daemon=True)
        proxy_thread.start()
        proxy = PortalProxy(
            "127.0.0.1",
            listener.getsockname()[1],
            username="worker",
            password="secret",
            scheme="socks5",
        )
        bridge = Socks5HttpBridge(proxy)
        bridge.start()
        try:
            endpoint = urlsplit(bridge.server_url)
            with socket.create_connection(
                (endpoint.hostname or "", endpoint.port or 0), timeout=5
            ) as client:
                client.sendall(
                    b"CONNECT portal.example:443 HTTP/1.1\r\n"
                    b"Host: portal.example:443\r\n\r\n"
                )
                response = client.recv(4096)
                self.assertIn(b"200 Connection Established", response)
                client.sendall(b"encrypted-payload")
                self.assertEqual(
                    b"encrypted-payload",
                    receive_exact(client, len(b"encrypted-payload")),
                )
        finally:
            bridge.close()
            listener.close()
            proxy_thread.join(timeout=5)

        if "error" in state:
            raise state["error"]  # type: ignore[misc]
        self.assertEqual(b"\x05\x01\x02", state["greeting"])
        self.assertEqual((1, b"worker", b"secret"), state["auth"])
        self.assertEqual(b"\x05\x01\x00\x03", state["connect_header"])
        self.assertEqual("portal.example", state["target_host"])
        self.assertEqual(443, state["target_port"])


if __name__ == "__main__":
    unittest.main()

"""Ponte HTTP local para proxies SOCKS5 autenticadas.

Chromium não aceita autenticação SOCKS5 diretamente. A ponte escuta somente
no loopback, recebe ``CONNECT`` sem credenciais e abre o túnel autenticado no
endpoint operacional. Nenhum endereço ou segredo é registrado.
"""

from __future__ import annotations

import socket
import socketserver
import struct
import threading
from contextlib import suppress

from services.proxy import PortalProxy


class ProxyBridgeError(RuntimeError):
    pass


def _receive_exact(connection: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = connection.recv(size - len(chunks))
        if not chunk:
            raise ProxyBridgeError("Conexão SOCKS5 encerrada prematuramente.")
        chunks.extend(chunk)
    return bytes(chunks)


def _read_connect_request(connection: socket.socket) -> tuple[str, int, bytes]:
    data = bytearray()
    while b"\r\n\r\n" not in data:
        chunk = connection.recv(4096)
        if not chunk:
            raise ProxyBridgeError("Requisição de proxy vazia.")
        data.extend(chunk)
        if len(data) > 65_536:
            raise ProxyBridgeError("Cabeçalho de proxy excedeu o limite.")
    header, pending = bytes(data).split(b"\r\n\r\n", 1)
    try:
        method, authority, _version = header.split(b"\r\n", 1)[0].decode(
            "ascii"
        ).split(" ", 2)
        host, port_text = authority.rsplit(":", 1)
        host = host.strip("[]")
        port = int(port_text)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProxyBridgeError("Requisição CONNECT inválida.") from exc
    if method.upper() != "CONNECT" or not host or not 1 <= port <= 65_535:
        raise ProxyBridgeError("Somente CONNECT válido é aceito.")
    return host, port, pending


def _open_socks5_tunnel(
    proxy: PortalProxy, target_host: str, target_port: int
) -> socket.socket:
    upstream = socket.create_connection((proxy.host, proxy.port), timeout=20)
    try:
        method = b"\x02" if proxy.username is not None else b"\x00"
        upstream.sendall(b"\x05\x01" + method)
        if _receive_exact(upstream, 2) != b"\x05" + method:
            raise ProxyBridgeError("Método SOCKS5 recusado.")
        if proxy.username is not None:
            username = proxy.username.encode()
            password = (proxy.password or "").encode()
            if not 1 <= len(username) <= 255 or len(password) > 255:
                raise ProxyBridgeError("Credencial SOCKS5 fora do limite.")
            upstream.sendall(
                b"\x01"
                + bytes((len(username),))
                + username
                + bytes((len(password),))
                + password
            )
            if _receive_exact(upstream, 2) != b"\x01\x00":
                raise ProxyBridgeError("Autenticação SOCKS5 recusada.")
        encoded_host = target_host.encode("idna")
        if not 1 <= len(encoded_host) <= 255:
            raise ProxyBridgeError("Destino SOCKS5 inválido.")
        upstream.sendall(
            b"\x05\x01\x00\x03"
            + bytes((len(encoded_host),))
            + encoded_host
            + struct.pack("!H", target_port)
        )
        version, result, _reserved, address_type = _receive_exact(upstream, 4)
        if version != 5 or result != 0:
            raise ProxyBridgeError("Túnel SOCKS5 recusado.")
        if address_type == 1:
            _receive_exact(upstream, 4)
        elif address_type == 3:
            _receive_exact(upstream, _receive_exact(upstream, 1)[0])
        elif address_type == 4:
            _receive_exact(upstream, 16)
        else:
            raise ProxyBridgeError("Resposta SOCKS5 inválida.")
        _receive_exact(upstream, 2)
        upstream.settimeout(None)
        return upstream
    except Exception:
        upstream.close()
        raise


def _pump(source: socket.socket, destination: socket.socket) -> None:
    try:
        while chunk := source.recv(65_536):
            destination.sendall(chunk)
    except OSError:
        pass
    finally:
        with suppress(OSError):
            destination.shutdown(socket.SHUT_WR)


class _BridgeHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        upstream: socket.socket | None = None
        try:
            host, port, pending = _read_connect_request(self.request)
            upstream = _open_socks5_tunnel(self.server.upstream_proxy, host, port)
            self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            if pending:
                upstream.sendall(pending)
            client_to_upstream = threading.Thread(
                target=_pump,
                args=(self.request, upstream),
                daemon=True,
            )
            client_to_upstream.start()
            _pump(upstream, self.request)
            client_to_upstream.join(timeout=1)
        except (OSError, ProxyBridgeError):
            with suppress(OSError):
                self.request.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
        finally:
            if upstream is not None:
                upstream.close()


class _BridgeServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, proxy: PortalProxy) -> None:
        self.upstream_proxy = proxy
        super().__init__(("127.0.0.1", 0), _BridgeHandler)


class Socks5HttpBridge:
    def __init__(self, proxy: PortalProxy) -> None:
        if proxy.scheme != "socks5" or proxy.username is None:
            raise ValueError("A ponte exige uma proxy SOCKS5 autenticada.")
        self._server = _BridgeServer(proxy)
        self._thread: threading.Thread | None = None

    @property
    def server_url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="safeconsig-proxy-bridge",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        if self._thread is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        self._thread = None

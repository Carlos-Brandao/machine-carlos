"""Parsing seguro e compartilhado de proxies operacionais."""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit


@dataclass(frozen=True, slots=True)
class PortalProxy:
    host: str
    port: int
    username: str | None = field(default=None, repr=False)
    password: str | None = field(default=None, repr=False)
    scheme: str = "http"

    def playwright_settings(self) -> dict[str, str]:
        settings = {"server": f"{self.scheme}://{self.host}:{self.port}"}
        if self.username is not None:
            settings["username"] = self.username
            settings["password"] = self.password or ""
        return settings

    def twocaptcha_value(self) -> str:
        authority = f"{self.host}:{self.port}"
        if self.username is None:
            return authority
        return f"{self.username}:{self.password or ''}@{authority}"

    @property
    def twocaptcha_type(self) -> str:
        return "SOCKS5" if self.scheme == "socks5" else "HTTP"

    @property
    def requires_http_bridge(self) -> bool:
        return self.scheme == "socks5" and self.username is not None


def parse_proxy(raw: str) -> PortalProxy:
    """Aceita o formato legado HTTP ou URLs ``http``/``socks5``."""
    value = str(raw or "").strip()
    if not value:
        raise ValueError("proxy vazia")
    if "://" not in value:
        scheme = "http"
        parts = value.split(":", 3)
        if len(parts) == 2:
            host, port_text = parts
            username = password = None
        elif len(parts) == 4:
            host, port_text, username, password = parts
        else:
            raise ValueError("formato de proxy inválido")
    else:
        parsed = urlsplit(value)
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "socks5"} or not parsed.hostname:
            raise ValueError("protocolo de proxy inválido")
        host = parsed.hostname
        try:
            port_text = str(parsed.port or "")
        except ValueError as exc:
            raise ValueError("porta de proxy inválida") from exc
        username = unquote(parsed.username) if parsed.username is not None else None
        password = unquote(parsed.password) if parsed.password is not None else None
    host = host.strip()
    if not host:
        raise ValueError("host de proxy inválido")
    try:
        port = int(port_text)
    except (TypeError, ValueError) as exc:
        raise ValueError("porta de proxy inválida") from exc
    if not 1 <= port <= 65_535:
        raise ValueError("porta de proxy inválida")
    if (username is None) != (password is None):
        raise ValueError("credenciais de proxy incompletas")
    if username is not None and not username:
        raise ValueError("usuário de proxy inválido")
    return PortalProxy(
        host=host,
        port=port,
        username=username,
        password=password,
        scheme=scheme,
    )

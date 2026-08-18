"""Cliente único da Bot API do Telegram e notificações dos robôs."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import requests

from machine_admin.secret_store import get_runtime_secret


LOG = logging.getLogger(__name__)
TELEGRAM_API_TIMEOUT = 35


class TelegramAPIError(RuntimeError):
    """A Bot API respondeu, mas recusou a operação solicitada."""


class TelegramClient:
    """Único adaptador HTTP permitido para a Bot API neste projeto."""

    def __init__(self, token: str, session: requests.Session | None = None) -> None:
        token = token.strip()
        if not token:
            raise ValueError("TELEGRAM_BOT_TOKEN não configurado.")
        self.session = session or requests.Session()
        self.api_url = f"https://api.telegram.org/bot{token}"

    def call(self, method: str, payload: dict[str, Any]) -> Any:
        response = self.session.post(
            f"{self.api_url}/{method}", json=payload, timeout=TELEGRAM_API_TIMEOUT
        )
        response.raise_for_status()
        try:
            data = response.json()
        except ValueError as exc:
            raise TelegramAPIError("Telegram retornou uma resposta inválida.") from exc
        if not isinstance(data, dict) or not data.get("ok"):
            description = data.get("description") if isinstance(data, dict) else None
            raise TelegramAPIError(str(description or "Erro na Bot API."))
        return data.get("result")

    def send_message(
        self, chat_id: int, text: str, *, parse_mode: str | None = None
    ) -> Any:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        return self.call("sendMessage", payload)

    def send_document(
        self, chat_id: int, file_path: Path, caption: str = ""
    ) -> Any:
        with file_path.open("rb") as document:
            response = self.session.post(
                f"{self.api_url}/sendDocument",
                data={"chat_id": chat_id, "caption": caption, "parse_mode": "Markdown"},
                files={"document": document},
                timeout=TELEGRAM_API_TIMEOUT,
            )
        response.raise_for_status()
        try:
            data = response.json()
        except ValueError as exc:
            raise TelegramAPIError("Telegram retornou uma resposta inválida.") from exc
        if not isinstance(data, dict) or not data.get("ok"):
            description = data.get("description") if isinstance(data, dict) else None
            raise TelegramAPIError(str(description or "Erro na Bot API."))
        return data.get("result")


class TelegramNotifier:
    """Notificações tolerantes a falhas para os robôs de consulta.

    Usa o mesmo token do controlador. ``TELEGRAM_CHAT_ID`` permanece como
    fallback temporário para instalações existentes; prefira o nome explícito
    ``TELEGRAM_NOTIFICATION_CHAT_ID``.
    """

    def __init__(self, client: TelegramClient | None, chat_id: int | None) -> None:
        self.client = client
        self.chat_id = chat_id

    @classmethod
    def from_environment(cls) -> "TelegramNotifier":
        token = get_runtime_secret("TELEGRAM_BOT_TOKEN")
        raw_chat_id = (
            os.getenv("TELEGRAM_NOTIFICATION_CHAT_ID", "").strip()
            or os.getenv("TELEGRAM_CHAT_ID", "").strip()
            or next(
                (
                    value.strip()
                    for value in os.getenv("TELEGRAM_ALLOWED_USER_IDS", "").split(",")
                    if value.strip()
                ),
                "",
            )
        )
        if not token or not raw_chat_id:
            return cls(None, None)
        try:
            return cls(TelegramClient(token), int(raw_chat_id))
        except ValueError:
            LOG.warning("TELEGRAM_NOTIFICATION_CHAT_ID deve ser um ID numérico; notificações desativadas.")
            return cls(None, None)

    @classmethod
    def for_chat(cls, chat_id: int | None) -> "TelegramNotifier":
        """Cria um destino explícito, sem redirecionar dados para um fallback.

        Resultados de jobs contêm dados pessoais e só podem ser enviados ao
        chat associado ao pedido. O fallback histórico permanece apenas para
        notificações operacionais genéricas.
        """
        token = get_runtime_secret("TELEGRAM_BOT_TOKEN")
        if not token or chat_id is None:
            return cls(None, None)
        try:
            return cls(TelegramClient(token), int(chat_id))
        except (TypeError, ValueError):
            LOG.warning("Destino Telegram explícito inválido; envio desativado.")
            return cls(None, None)

    @property
    def enabled(self) -> bool:
        return self.client is not None and self.chat_id is not None

    def message(self, text: str) -> None:
        if not self.enabled:
            return
        try:
            self.client.send_message(self.chat_id, text, parse_mode="Markdown")
        except (requests.RequestException, TelegramAPIError):
            LOG.exception("Falha ao enviar notificação para o Telegram.")

    def document(self, file_path: Path, caption: str) -> bool:
        if not self.enabled:
            return False
        try:
            self.client.send_document(self.chat_id, file_path, caption)
            return True
        except (OSError, requests.RequestException, TelegramAPIError):
            LOG.exception("Falha ao enviar documento para o Telegram.")
            return False

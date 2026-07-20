"""Notificações externas opcionais usadas pelos bots."""

import os

import requests


def send_telegram_message(message: str) -> bool:
    """Envia uma notificação ao Telegram quando as credenciais estão configuradas.

    A ausência de configuração ou uma falha de rede não interrompe a consulta.
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=15,
        )
        response.raise_for_status()
        return True
    except requests.RequestException as exc:
        print(f"[Telegram] Não foi possível enviar a notificação: {exc}")
        return False

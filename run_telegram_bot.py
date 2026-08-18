"""Inicializa o controlador Telegram do Machine."""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from machine_admin.secret_store import configure_remote_secret_provider
from services.telegram_bot import ConfigurationError, Settings, TelegramBot
from workers.api_client import WorkerAPIClient


def main() -> None:
    parser = argparse.ArgumentParser(description="Controlador Telegram do Machine")
    parser.add_argument(
        "--set-commands",
        action="store_true",
        help="Registra os comandos no menu do Telegram e encerra.",
    )
    args = parser.parse_args()

    load_dotenv(Path(__file__).parent / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    backend_url = os.getenv("BACKEND_API_URL", "").strip().rstrip("/")
    backend_token = (
        os.getenv("TELEGRAM_BACKEND_API_TOKEN", "").strip()
        or os.getenv("BACKEND_API_TOKEN", "").strip()
    )
    if backend_url and backend_token:
        configure_remote_secret_provider(
            WorkerAPIClient(backend_url, backend_token).runtime_secret
        )

    try:
        bot = TelegramBot(Settings.from_environment())
    except ConfigurationError as exc:
        raise SystemExit(f"Erro de configuração: {exc}") from exc

    if args.set_commands:
        bot.set_commands()
        print("Comandos registrados com sucesso.")
        return

    bot.run_forever()


if __name__ == "__main__":
    main()

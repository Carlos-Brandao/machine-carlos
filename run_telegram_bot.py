"""Inicializa o controlador Telegram do Machine."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from dotenv import load_dotenv

from services.telegram_bot import ConfigurationError, Settings, TelegramBot


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

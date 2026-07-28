"""Entrada legada: o projeto agora usa systemd, não cron."""

from __future__ import annotations


def main() -> None:
    print("O agendamento por cron foi desativado para evitar execuções duplicadas.")
    print("Instale deploy/machine-backend.service e deploy/machine-scheduler.service.")
    print("Depois execute: sudo systemctl enable --now machine-backend machine-scheduler")


if __name__ == "__main__":
    main()

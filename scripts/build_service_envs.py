"""Deriva ambientes mínimos por processo a partir do .env mestre.

O arquivo mestre continua sendo a fonte de bootstrap/backup, mas não é
montado no diretório do release nem carregado pelos serviços. As listas abaixo
são deliberadamente fechadas: uma variável nova precisa de revisão explícita
antes de chegar a um processo.
"""

from __future__ import annotations

import argparse
import grp
import os
import re
import tempfile
from pathlib import Path

from dotenv import dotenv_values


COMMON_APP_KEYS = {
    "DATABASE_URL",
    "ADMIN_SESSION_SECRET",
    "APP_MASTER_KEY",
    "ADMIN_COOKIE_SECURE",
    "ADMIN_ALLOWED_HOSTS",
    "MAX_UPLOAD_BYTES",
}
SERVICE_KEYS: dict[str, set[str]] = {
    "backend": COMMON_APP_KEYS
    | {
        "ADMIN_HOST",
        "ADMIN_PORT",
        "BACKEND_HOST",
        "BACKEND_PORT",
        "BOOTSTRAP_ADMIN_EMAIL",
        "BOOTSTRAP_ADMIN_PASSWORD",
        # Necessário apenas durante a transição para o cofre do painel.
        "TWOCAPTCHA_API_KEY",
        "CONSIGX_HTTPS_PROXY",
        "TELEGRAM_BOT_TOKEN",
    },
    "worker": {
        "WORKER_API_URL",
        "WORKER_API_TOKEN",
        "BACKEND_API_URL",
        "TWOCAPTCHA_API_KEY",
        "CONSIGX_HTTPS_PROXY",
        "HEADLESS",
        "CAPTCHA_DEBUG",
        "WORKER_PLATFORMS",
    },
    "telegram": {
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_ALLOWED_USER_IDS",
        "BACKEND_API_URL",
        "TELEGRAM_BACKEND_API_TOKEN",
        # Fallback transitório aceito pelo controlador Telegram.
        "BACKEND_API_TOKEN",
    },
    "notifications": COMMON_APP_KEYS
    | {
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_NOTIFICATION_CHAT_ID",
        "TELEGRAM_CHAT_ID",
        "TELEGRAM_ALLOWED_USER_IDS",
    },
}
SERVICE_GROUPS = {
    "backend": "machine-backend",
    "worker": "machine-worker",
    "telegram": "machine-telegram",
    "notifications": "machine-notify",
}
PREFIX_KEYS: dict[str, tuple[str, ...]] = {
    "worker": ("WORKER_COUNT_",),
}
KEY_PATTERN = re.compile(r"^[A-Z_][A-Z0-9_]*$")


def _quote(value: str) -> str:
    if "\x00" in value or "\n" in value or "\r" in value:
        raise ValueError("valores multiline não são aceitos nos ambientes systemd")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _selected_values(
    source: dict[str, str | None],
    service: str,
) -> dict[str, str]:
    allowed = SERVICE_KEYS[service]
    prefixes = PREFIX_KEYS.get(service, ())
    selected = {
        key: str(value)
        for key, value in source.items()
        if value is not None
        and KEY_PATTERN.fullmatch(key)
        and (key in allowed or key.startswith(prefixes))
    }
    if service == "worker" and not selected.get("WORKER_API_TOKEN"):
        # Compatibilidade controlada com instalações antigas. O token deve
        # ser substituído depois por um token limitado a workers:execute.
        legacy_token = source.get("BACKEND_API_TOKEN")
        if legacy_token:
            selected["WORKER_API_TOKEN"] = str(legacy_token)
    return selected


def build_service_envs(
    source_path: Path,
    output_dir: Path,
    owner_group: str = "machine",
) -> None:
    if not source_path.is_file():
        raise RuntimeError(f".env mestre ausente: {source_path}")
    owner_group_id = grp.getgrnam(owner_group).gr_gid
    source = dict(dotenv_values(source_path, interpolate=False))
    output_dir.mkdir(mode=0o711, parents=True, exist_ok=True)
    os.chown(output_dir, 0, owner_group_id)
    os.chmod(output_dir, 0o711)

    for service in sorted(SERVICE_KEYS):
        service_group_id = grp.getgrnam(SERVICE_GROUPS[service]).gr_gid
        values = _selected_values(source, service)
        content = "".join(f"{key}={_quote(values[key])}\n" for key in sorted(values))
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{service}.",
            dir=output_dir,
            text=True,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.chown(temporary_name, 0, service_group_id)
            os.chmod(temporary_name, 0o640)
            os.replace(temporary_name, output_dir / f"{service}.env")
        except Exception:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--owner-group", default="machine")
    args = parser.parse_args()
    os.umask(0o027)
    build_service_envs(args.source, args.output_dir, args.owner_group)
    print(f"Ambientes mínimos atualizados em {args.output_dir}")


if __name__ == "__main__":
    main()

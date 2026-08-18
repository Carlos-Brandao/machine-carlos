"""Backup consistente do PostgreSQL e dos arquivos persistentes da Machine."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
from datetime import UTC, datetime
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy.engine import make_url


LABEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
PERSISTENT_NAMES = (
    ".env",
    "storage",
    "job_logs",
    "data",
    "completed",
    "debug_captchas",
)


def _specific_absolute_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or path == Path("/"):
        raise ValueError("--root deve ser um caminho absoluto específico")
    return path.resolve()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _pg_dump(database_url: str, destination: Path) -> None:
    url = make_url(database_url)
    if not url.drivername.startswith("postgresql") or not url.database:
        raise RuntimeError("DATABASE_URL precisa apontar para PostgreSQL")
    command = ["pg_dump", "--format=custom", "--file", str(destination)]
    if url.host:
        command.extend(("--host", url.host))
    if url.port:
        command.extend(("--port", str(url.port)))
    if url.username:
        command.extend(("--username", url.username))
    command.extend(("--dbname", url.database))
    environment = os.environ.copy()
    if url.password:
        environment["PGPASSWORD"] = url.password
    if sslmode := url.query.get("sslmode"):
        environment["PGSSLMODE"] = str(sslmode)
    subprocess.run(command, check=True, env=environment)


def _archive_shared(shared: Path, destination: Path) -> list[str]:
    included: list[str] = []
    with tarfile.open(destination, "w:gz", dereference=False) as archive:
        for name in PERSISTENT_NAMES:
            source = shared / name
            if not source.exists():
                continue
            archive.add(source, arcname=name, recursive=True)
            included.append(name)
    return included


def _prune_backups(backups: Path, keep: int) -> None:
    candidates = sorted(
        (
            path
            for path in backups.iterdir()
            if path.is_dir()
            and not path.is_symlink()
            and not path.name.startswith(".")
            and (path / "manifest.json").is_file()
        ),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    for old in candidates[keep:]:
        shutil.rmtree(old)


def create_backup(root: Path, label: str, keep: int) -> Path:
    if not LABEL_PATTERN.fullmatch(label):
        raise ValueError("label de backup inválido")
    if not 2 <= keep <= 60:
        raise ValueError("--keep deve estar entre 2 e 60")

    shared = root / "shared"
    env_file = shared / ".env"
    if not env_file.is_file():
        raise RuntimeError(f"Arquivo de ambiente ausente: {env_file}")
    values = dotenv_values(env_file)
    database_url = str(values.get("DATABASE_URL") or os.getenv("DATABASE_URL") or "")
    if not database_url:
        raise RuntimeError("DATABASE_URL não configurada")

    backups = root / "backups"
    backups.mkdir(mode=0o700, parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    final = backups / f"{timestamp}-{label}"
    staging = backups / f".{timestamp}-{label}.tmp"
    if staging.exists() or final.exists():
        raise RuntimeError("destino de backup já existe")
    staging.mkdir(mode=0o700)

    try:
        database_dump = staging / "database.dump"
        shared_archive = staging / "shared.tar.gz"
        _pg_dump(database_url, database_dump)
        included = _archive_shared(shared, shared_archive)
        current = (root / "current").resolve() if (root / "current").exists() else None
        manifest = {
            "created_at": datetime.now(UTC).isoformat(),
            "label": label,
            "active_release": str(current) if current else None,
            "included": included,
            "database_sha256": _sha256(database_dump),
            "shared_sha256": _sha256(shared_archive),
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        staging.rename(final)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    _prune_backups(backups, keep)
    return final


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--keep", type=int, default=7)
    args = parser.parse_args()
    os.umask(0o077)
    destination = create_backup(
        _specific_absolute_path(args.root),
        args.label,
        args.keep,
    )
    print(f"Backup criado: {destination}")


if __name__ == "__main__":
    main()

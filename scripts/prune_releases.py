"""Remove releases antigos sem tocar no release ativo ou em dados compartilhados."""

from __future__ import annotations

import argparse
import re
import shutil
from pathlib import Path


RELEASE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")


def prune(root_value: str, keep: int) -> list[Path]:
    root = Path(root_value)
    if not root.is_absolute() or root == Path("/"):
        raise ValueError("--root deve ser um caminho absoluto específico")
    if not 2 <= keep <= 20:
        raise ValueError("--keep deve estar entre 2 e 20")
    root = root.resolve()
    releases = (root / "releases").resolve()
    if releases.parent != root or not releases.is_dir():
        raise RuntimeError("diretório de releases inválido")

    current = (root / "current").resolve() if (root / "current").exists() else None
    candidates = sorted(
        (
            path
            for path in releases.iterdir()
            if path.is_dir()
            and not path.is_symlink()
            and RELEASE_PATTERN.fullmatch(path.name)
            and (path / "release.json").is_file()
            and (path / ".ready").is_file()
        ),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    retained: set[Path] = set()
    if current and current.parent == releases:
        retained.add(current)
    for candidate in candidates:
        if len(retained) >= keep:
            break
        retained.add(candidate)

    removed: list[Path] = []
    for release in candidates:
        if release in retained:
            continue
        shutil.rmtree(release)
        removed.append(release)
    return removed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--keep", type=int, default=5)
    args = parser.parse_args()
    for removed in prune(args.root, args.keep):
        print(f"Release antigo removido: {removed.name}")


if __name__ == "__main__":
    main()
